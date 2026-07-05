"""飞书多维表格通用客户端。

只做「与飞书打交道」这件事：鉴权、列出/读/写记录、读字段。
业务字段映射与语义在 services/ 中处理，adapter 保持通用。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import requests
from loguru import logger

from app.config import get_settings

_INVALID_TOKEN_CODE = 99991663


class FeishuBitableClient:
    base_url = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str, app_token: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self._token = ""
        self._fields_cache: dict[str, list[dict[str, Any]]] = {}

    # ---------- 鉴权 ----------
    def _get_token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        resp = requests.post(
            f"{self.base_url}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"获取飞书 token 失败: {payload}")
        self._token = str(payload["tenant_access_token"])
        return self._token

    def _request(self, method: str, path: str, *, retry: bool = True, **kwargs: Any) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._get_token()}"
        resp = requests.request(method, f"{self.base_url}{path}", headers=headers, timeout=30, **kwargs)
        try:
            payload = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise
        if retry and (payload.get("code") == _INVALID_TOKEN_CODE or "Invalid access token" in str(payload.get("msg", ""))):
            logger.warning("飞书 token 失效，刷新后重试 - {} {}", method, path)
            self._get_token(force=True)
            return self._request(method, path, retry=False, **kwargs)
        resp.raise_for_status()
        if payload.get("code") != 0:
            raise RuntimeError(f"飞书接口失败: {payload}")
        return payload

    # ---------- 字段 ----------
    def get_fields(self, table_id: str) -> list[dict[str, Any]]:
        if table_id in self._fields_cache:
            return self._fields_cache[table_id]
        payload = self._request(
            "GET",
            f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields",
            params={"page_size": 100},
        )
        fields = payload.get("data", {}).get("items", [])
        self._fields_cache[table_id] = fields
        return fields

    def resolve_field(self, table_id: str, candidates: list[str]) -> str | None:
        names = {str(f.get("field_name")) for f in self.get_fields(table_id)}
        for candidate in candidates:
            if candidate in names:
                return candidate
        return None

    # ---------- 记录 ----------
    def list_records(self, table_id: str, filter_: str | None = None, page_size: int = 200) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            if filter_:
                params["filter"] = filter_
            payload = self._request(
                "GET",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records",
                params=params,
            )
            data = payload.get("data", {})
            records.extend(data.get("items", []))
            page_token = data.get("page_token")
            if not data.get("has_more"):
                break
        return records

    def create_record(self, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records",
            json={"fields": fields},
        )
        return payload.get("data", {}).get("record", {})

    def update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = self._request(
            "PUT",
            f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/{record_id}",
            json={"fields": fields},
        )
        return payload.get("data", {}).get("record", {})


@lru_cache
def get_feishu_client() -> FeishuBitableClient:
    settings = get_settings()
    return FeishuBitableClient(
        app_id=settings.feishu_vn_app_id,
        app_secret=settings.feishu_vn_app_secret,
        app_token=settings.feishu_vn_bitable_app_token,
    )
