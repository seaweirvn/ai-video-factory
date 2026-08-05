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

# 建字段时 ui_type -> 飞书字段 type 编号
_UI_TYPE_TO_FIELD_TYPE = {
    "text": 1,
    "number": 2,
    "singleselect": 3,
    "multiselect": 4,
    "datetime": 5,
    "checkbox": 7,
    "user": 11,
    "url": 15,
}


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

    def create_field(self, table_id: str, field_name: str, ui_type: str = "text") -> dict[str, Any]:
        field_type = _UI_TYPE_TO_FIELD_TYPE.get(ui_type.casefold(), 1)
        payload = self._request(
            "POST",
            f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields",
            json={"field_name": field_name, "type": field_type},
        )
        self._fields_cache.pop(table_id, None)  # 建完失效缓存，后续 resolve 能看到新列
        logger.info("飞书新建字段 - {} ({})", field_name, ui_type)
        return payload.get("data", {}).get("field", {})

    def ensure_field(self, table_id: str, candidates: list[str], ui_type: str = "text") -> str:
        """字段存在则返回真实名，否则用首选名新建并返回。"""
        existing = self.resolve_field(table_id, candidates)
        if existing:
            return existing
        self.create_field(table_id, candidates[0], ui_type)
        return candidates[0]

    def field_ui_type(self, table_id: str, field_name: str) -> str:
        field = next(
            (f for f in self.get_fields(table_id) if f.get("field_name") == field_name), {}
        )
        return str(field.get("ui_type") or field.get("uiType") or "").casefold()

    def format_value(self, table_id: str, field_name: str, value: Any) -> Any:
        """按目标字段的 ui_type 把 Python 值转成飞书可接受的写入格式。"""
        ui_type = self.field_ui_type(table_id, field_name)
        if value is None:
            return value
        if ui_type in ("number",):
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        if ui_type in ("checkbox",):
            return bool(value)
        if ui_type in ("url",):
            return {"link": str(value), "text": str(value)}
        if ui_type in ("text", "singleline", "multiline", "barcode", ""):
            return str(value)
        return value

    @staticmethod
    def cell_link(cell: Any) -> str:
        """从文本/超链接单元格里取出第一个真实 URL（配合 text_field_as_array=true）。"""
        if cell is None:
            return ""
        if isinstance(cell, str):
            return cell if cell.startswith("http") else ""
        if isinstance(cell, dict):
            return str(cell.get("link") or "")
        if isinstance(cell, list):
            for item in cell:
                if isinstance(item, dict) and item.get("link"):
                    return str(item["link"])
            return ""
        return ""

    @staticmethod
    def cell_text(cell: Any) -> str:
        """把飞书单元格值（可能是 str / list / dict）读成纯文本。"""
        if cell is None:
            return ""
        if isinstance(cell, str):
            return cell
        if isinstance(cell, (int, float, bool)):
            return str(cell)
        if isinstance(cell, dict):
            for key in ("text", "link", "name"):
                if key in cell:
                    return str(cell[key])
            return ""
        if isinstance(cell, list):
            parts = [FeishuBitableClient.cell_text(item) for item in cell]
            return " ".join(p for p in parts if p)
        return str(cell)

    # ---------- 记录 ----------
    def list_records(
        self,
        table_id: str,
        filter_: str | None = None,
        page_size: int = 200,
        text_field_as_array: bool = False,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if text_field_as_array:
                # 富文本/超链接字段返回分段数组（含 link），否则只拿到纯文本。
                params["text_field_as_array"] = "true"
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

    def delete_record(self, table_id: str, record_id: str) -> bool:
        payload = self._request(
            "DELETE",
            f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/{record_id}",
        )
        return bool(payload.get("data", {}).get("deleted", True))


@lru_cache
def get_feishu_client() -> FeishuBitableClient:
    settings = get_settings()
    return FeishuBitableClient(
        app_id=settings.feishu_vn_app_id,
        app_secret=settings.feishu_vn_app_secret,
        app_token=settings.feishu_vn_bitable_app_token,
    )


@lru_cache
def make_feishu_client(app_token: str) -> FeishuBitableClient:
    """为指定 bitable（如素材库所在的“营销”知识库表）构造客户端。

    tenant_access_token 是应用级的，可跨 bitable 复用同一 app_id/secret。
    """
    settings = get_settings()
    return FeishuBitableClient(
        app_id=settings.feishu_vn_app_id,
        app_secret=settings.feishu_vn_app_secret,
        app_token=app_token,
    )
