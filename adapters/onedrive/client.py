"""OneDrive（Microsoft Graph）客户端。

阶段 0 提供三件事：
- 设备码登录 + 凭据持久化（沿用 seaweir-video 已跑通的方案）。
- 上传本地文件并生成只读分享链接。
- 按分享链接下载文件到本地临时目录（工厂摄取素材用）。
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import requests
from loguru import logger

from app.config import get_settings

# 上传分片必须是 320 KiB 的整数倍。
_UPLOAD_CHUNK_SIZE = 320 * 1024 * 10
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class OneDriveClient:
    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        target_folder: str,
        auth_record_path: Path,
        cache_name: str,
        scopes: str,
        link_type: str,
        link_scope: str,
    ) -> None:
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.target_folder = "/" + target_folder.strip("/")
        self.auth_record_path = Path(auth_record_path)
        self.cache_name = cache_name
        self.scopes = [s.strip() for s in scopes.split(",") if s.strip()]
        self.link_type = link_type
        self.link_scope = link_scope
        self._credential = None

    # ---------- 凭据 ----------
    def _get_credential(self):
        """构建一次并复用；azure-identity 会在实例内缓存/静默刷新 token。"""
        if self._credential is None:
            self._credential = self._build_credential()
        return self._credential

    def _build_credential(self):
        from azure.identity import (
            AuthenticationRecord,
            DeviceCodeCredential,
            TokenCachePersistenceOptions,
        )

        cache_options = TokenCachePersistenceOptions(name=self.cache_name)

        def prompt_callback(verification_uri: str, user_code: str, expires_on: datetime) -> None:
            logger.info("=" * 60)
            logger.info("请在浏览器打开: {}", verification_uri)
            logger.info("并输入验证码: {}", user_code)
            logger.info("=" * 60)

        if self.auth_record_path.exists():
            record = AuthenticationRecord.deserialize(self.auth_record_path.read_text(encoding="utf-8"))
            logger.info("检测到已保存的 OneDrive 凭据，尝试静默刷新")
            return DeviceCodeCredential(
                client_id=self.client_id,
                tenant_id=self.tenant_id,
                authentication_record=record,
                cache_persistence_options=cache_options,
                prompt_callback=prompt_callback,
            )

        logger.info("首次运行：需要 OneDrive 设备码登录")
        credential = DeviceCodeCredential(
            client_id=self.client_id,
            tenant_id=self.tenant_id,
            cache_persistence_options=cache_options,
            prompt_callback=prompt_callback,
        )
        record = credential.authenticate(scopes=self.scopes, enable_cae=True)
        self.auth_record_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_record_path.write_text(record.serialize(), encoding="utf-8")
        logger.info("OneDrive 登录成功，凭据已保存到 {}", self.auth_record_path)
        return credential

    def _access_token(self) -> str:
        credential = self._get_credential()
        token = credential.get_token(*self.scopes)
        return token.token

    # ---------- 下载（按分享链接） ----------
    def download_share_link(self, share_url: str, dest_path: Path) -> Path:
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        share_id = self._encode_share_url(share_url)
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        url = f"{_GRAPH_BASE}/shares/{share_id}/driveItem/content"
        logger.info("按分享链接下载 OneDrive 文件 -> {}", dest_path)
        with requests.get(url, headers=headers, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with dest_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        logger.info("下载完成 - {} ({} 字节)", dest_path, dest_path.stat().st_size)
        return dest_path

    def get_share_item_metadata(self, share_url: str) -> dict:
        """按分享链接读取 driveItem 元数据（含 video facet：时长/分辨率/FPS/音频）。

        不下载文件本体，只取元数据，适合大批量素材快速读时长。
        """
        share_id = self._encode_share_url(share_url)
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        url = f"{_GRAPH_BASE}/shares/{share_id}/driveItem"
        resp = requests.get(
            url,
            headers=headers,
            params={"$select": "id,name,size,file,video,audio"},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _encode_share_url(share_url: str) -> str:
        encoded = base64.urlsafe_b64encode(share_url.encode("utf-8")).decode("utf-8")
        return "u!" + encoded.rstrip("=")

    # ---------- 上传 + 分享 ----------
    def upload_and_share(self, local_path: Path) -> str:
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"待上传文件不存在: {local_path}")
        return asyncio.run(self._upload_and_share_async(local_path))

    async def _upload_and_share_async(self, local_path: Path) -> str:
        from msgraph import GraphServiceClient
        from msgraph.generated.drives.item.items.item.create_upload_session.create_upload_session_post_request_body import (
            CreateUploadSessionPostRequestBody,
        )
        from msgraph.generated.models.drive_item import DriveItem
        from msgraph.generated.models.drive_item_uploadable_properties import (
            DriveItemUploadableProperties,
        )
        from msgraph_core.tasks import LargeFileUploadTask

        credential = self._build_credential()
        client = GraphServiceClient(credentials=credential, scopes=self.scopes)

        drive = await client.me.drive.get()
        if drive is None or not drive.id:
            raise RuntimeError("无法获取 OneDrive drive，请确认账号已授权")
        drive_id = drive.id

        item_path = f"root:{self.target_folder}/{local_path.name}:"
        upload_props = DriveItemUploadableProperties(
            additional_data={"@microsoft.graph.conflictBehavior": "replace"}
        )
        session_body = CreateUploadSessionPostRequestBody(item=upload_props)
        upload_session = (
            await client.drives.by_drive_id(drive_id)
            .items.by_drive_item_id(item_path)
            .create_upload_session.post(session_body)
        )

        file_stream = BytesIO(local_path.read_bytes())
        total = local_path.stat().st_size

        def progress_callback(uploaded_range) -> None:
            try:
                uploaded = uploaded_range[1]
            except (TypeError, IndexError):
                uploaded = uploaded_range
            logger.info("上传进度 {}/{} 字节", uploaded, total)

        task = LargeFileUploadTask(
            upload_session,
            client.request_adapter,
            file_stream,
            parsable_factory=DriveItem,
            max_chunk_size=_UPLOAD_CHUNK_SIZE,
        )
        result = await task.upload(progress_callback)

        item = getattr(result, "item_response", None)
        item_id = getattr(item, "id", None)
        if not item_id:
            raise RuntimeError("上传完成但未拿到 OneDrive 文件 ID")

        share_link = await self._create_share_link(client, drive_id, item_id)
        logger.info("上传完成，分享链接: {}", share_link)
        return share_link

    async def _create_share_link(self, client, drive_id: str, item_id: str) -> str:
        from msgraph.generated.drives.item.items.item.create_link.create_link_post_request_body import (
            CreateLinkPostRequestBody,
        )

        scopes_to_try = [self.link_scope]
        if self.link_scope == "anonymous":
            scopes_to_try.append("organization")

        last_exc: Exception | None = None
        for scope in scopes_to_try:
            try:
                body = CreateLinkPostRequestBody(type=self.link_type, scope=scope)
                permission = (
                    await client.drives.by_drive_id(drive_id)
                    .items.by_drive_item_id(item_id)
                    .create_link.post(body)
                )
                link = getattr(getattr(permission, "link", None), "web_url", None)
                if not link:
                    raise RuntimeError(f"createLink 未返回链接: scope={scope}")
                if scope != self.link_scope:
                    logger.warning("anonymous 链接不可用，已回退为 {}", scope)
                return link
            except Exception as exc:  # noqa: BLE001 - 需要在多种 scope 间回退
                last_exc = exc
                logger.warning("生成 {} 分享链接失败: {}", scope, exc)

        raise RuntimeError(f"生成分享链接失败: {last_exc}") from last_exc


@lru_cache
def get_onedrive_client() -> OneDriveClient:
    s = get_settings()
    return OneDriveClient(
        client_id=s.onedrive_client_id,
        tenant_id=s.onedrive_tenant_id,
        target_folder=s.onedrive_target_folder,
        auth_record_path=s.onedrive_auth_record_path,
        cache_name=s.onedrive_token_cache_name,
        scopes=s.onedrive_scopes,
        link_type=s.onedrive_link_type,
        link_scope=s.onedrive_link_scope,
    )
