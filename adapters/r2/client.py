"""Cloudflare R2（S3 兼容）对象存储客户端。"""

from __future__ import annotations

import mimetypes
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import boto3
import requests
from botocore.config import Config

from app.config import get_settings


class R2Client:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_domain: str,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.bucket = bucket.strip()
        self.public_domain = public_domain.rstrip("/")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if not all(
                (self.endpoint_url, self.access_key_id, self.secret_access_key, self.bucket)
            ):
                raise RuntimeError("R2 配置不完整，请检查 endpoint/access key/secret/bucket")
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name="auto",
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 4, "mode": "standard"},
                ),
            )
        return self._client

    @staticmethod
    def normalize_key(key: str) -> str:
        return str(key or "").replace("\\", "/").lstrip("/")

    def check_connection(self) -> dict:
        """只读检查 Bucket 权限，返回少量状态信息。"""
        self.client.head_bucket(Bucket=self.bucket)
        result = self.client.list_objects_v2(Bucket=self.bucket, MaxKeys=1)
        return {
            "bucket": self.bucket,
            "accessible": True,
            "has_objects": bool(result.get("Contents")),
            "is_truncated": bool(result.get("IsTruncated")),
        }

    def iter_objects(self, prefix: str = ""):
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.normalize_key(prefix)):
            yield from page.get("Contents", [])

    def head_object(self, key: str) -> dict:
        return self.client.head_object(Bucket=self.bucket, Key=self.normalize_key(key))

    def upload_file(
        self, local_path: Path, key: str, *, content_type: str = ""
    ) -> str:
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(f"待上传文件不存在: {path}")
        object_key = self.normalize_key(key)
        mime = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.client.upload_file(
            str(path),
            self.bucket,
            object_key,
            ExtraArgs={"ContentType": mime},
        )
        return self.public_url(object_key)

    def download_key(self, key: str, dest_path: Path) -> Path:
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, self.normalize_key(key), str(dest))
        return dest

    def key_from_public_url(self, url: str) -> str:
        if not self.public_domain:
            return ""
        expected = urlparse(self.public_domain)
        actual = urlparse(str(url or ""))
        if actual.netloc.casefold() != expected.netloc.casefold():
            return ""
        base_path = expected.path.rstrip("/")
        path = actual.path
        if base_path and not path.startswith(base_path + "/"):
            return ""
        return self.normalize_key(unquote(path[len(base_path) :]))

    def download_url(self, url: str, dest_path: Path) -> Path:
        """优先把自有公开域名还原为 key 后走 S3 下载；其它 HTTP URL 直接流式下载。"""
        key = self.key_from_public_url(url)
        if key:
            return self.download_key(key, dest_path)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, stream=True, timeout=300) as response:
            response.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        return dest

    def public_url(self, key: str) -> str:
        if not self.public_domain:
            raise RuntimeError("未配置 R2_PUBLIC_DOMAIN，无法生成飞书可用公开链接")
        encoded = "/".join(quote(part, safe="") for part in self.normalize_key(key).split("/"))
        return f"{self.public_domain}/{encoded}"


@lru_cache
def get_r2_client() -> R2Client:
    settings = get_settings()
    return R2Client(
        endpoint_url=settings.r2_endpoint_url,
        access_key_id=settings.r2_access_key_id,
        secret_access_key=settings.r2_secret_access_key,
        bucket=settings.r2_bucket,
        public_domain=settings.r2_public_domain,
    )
