"""飞书创作者视频表适配器（KOL 下载链路专用）。

复用工厂通用 FeishuBitableClient 做鉴权/读写，这里只负责该表的字段语义：
读「待下载」行、回写下载勾选 / Video ID / OneDrive 链接。

字段名为越南表的双语命名（如 "Link/ Link video"），做「去空格+大小写不敏感」匹配，
容忍表头细微差异，找不到关键字段才报错。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from adapters.feishu.client import FeishuBitableClient
from services.kol.models import DownloadedVideo, SourceVideo

# 各语义字段的候选名（按优先级）
_SOURCE_URL = ["Link/ Link video"]
_TITLE = ["Video clip/ Tên clip", "标题"]
_DOWNLOAD = ["Download/ Tải xuống", "下载"]
_FEATURED = ["Featured video/ Video chất lượng cao"]
_TIKTOK_ID = ["ID Tiktok / tên tiktok"]
_VIDEO_ID = ["Video ID"]
_ONEDRIVE = [
    "Video Link",
    "ONEDRIVE LINK",
    "OneDrive Link",
    "ONEDRIVE_LINK",
]
_RELEASE = ["Video release date/Ngày đăng video", "Video release date", "Ngày đăng video"]


class KolVideoTable:
    def __init__(self, client: FeishuBitableClient, table_id: str) -> None:
        self.client = client
        self.table_id = table_id
        self._names: list[str] | None = None

    # ---------- 字段解析（去空格 + 大小写不敏感） ----------
    def _field_names(self) -> list[str]:
        if self._names is None:
            self._names = [str(f.get("field_name")) for f in self.client.get_fields(self.table_id)]
        return self._names

    def _resolve(self, candidates: list[str]) -> str:
        name = self._resolve_optional(candidates)
        if not name:
            raise RuntimeError(f"飞书创作者表缺少字段: {', '.join(candidates)}")
        return name

    def _resolve_optional(self, candidates: list[str]) -> str:
        names = self._field_names()
        for cand in candidates:
            if cand in names:
                return cand
            key = cand.replace(" ", "").casefold()
            for n in names:
                if n.replace(" ", "").casefold() == key:
                    return n
        return ""

    # ---------- 读「待下载」 ----------
    def fetch_pending(self) -> list[SourceVideo]:
        if not self.table_id:
            raise RuntimeError("未配置 FEISHU_VN_KOL_VIDEO_TABLE_ID")
        url_field = self._resolve(_SOURCE_URL)
        download_field = self._resolve(_DOWNLOAD)
        title_field = self._resolve_optional(_TITLE)
        featured_field = self._resolve_optional(_FEATURED)
        tiktok_field = self._resolve_optional(_TIKTOK_ID)
        release_field = self._resolve_optional(_RELEASE)

        records = self.client.list_records(self.table_id)
        out: list[SourceVideo] = []
        for rec in records:
            fields = rec.get("fields", {})
            if self._is_checked(fields.get(download_field)):
                continue
            source_url = self._text(fields.get(url_field))
            if not source_url:
                logger.warning("跳过无视频链接记录 - record_id={}", rec.get("record_id"))
                continue
            out.append(
                SourceVideo(
                    record_id=str(rec["record_id"]),
                    source_url=source_url,
                    title=self._text(fields.get(title_field)) if title_field else "",
                    tiktok_id=self._text(fields.get(tiktok_field)) if tiktok_field else "",
                    is_featured=(
                        self._text(fields.get(featured_field)).strip().casefold() == "good"
                        if featured_field
                        else False
                    ),
                    release_ms=self._as_ms(fields.get(release_field)) if release_field else None,
                    raw=rec,
                )
            )
        logger.info("飞书待下载 KOL 视频 {} 条", len(out))
        return out

    # ---------- 回写下载结果 ----------
    def mark_downloaded(self, video: DownloadedVideo, onedrive_link: str = "") -> None:
        download_field = self._resolve(_DOWNLOAD)
        video_id_field = self._resolve_optional(_VIDEO_ID)
        onedrive_field = self._resolve_optional(_ONEDRIVE) if onedrive_link else ""

        minimal: dict[str, Any] = {download_field: True}
        fields: dict[str, Any] = dict(minimal)
        if video.video_id and video_id_field:
            fields[video_id_field] = video.video_id
        if onedrive_link and onedrive_field:
            fields[onedrive_field] = self._format_link(onedrive_field, onedrive_link)

        try:
            self.client.update_record(self.table_id, video.record_id, fields)
        except Exception as exc:  # noqa: BLE001 - 回写失败时退化为只勾下载，避免整批中断
            logger.warning("飞书回写失败，尝试仅更新下载勾选 - record_id={} err={}", video.record_id, exc)
            if fields != minimal:
                self.client.update_record(self.table_id, video.record_id, minimal)
            else:
                raise
        logger.info(
            "已回写飞书 - record_id={} video_id={} onedrive={}",
            video.record_id, video.video_id, bool(onedrive_link),
        )

    def _format_link(self, field_name: str, link: str) -> Any:
        if self.client.field_ui_type(self.table_id, field_name).casefold() == "url":
            return {"link": link, "text": "Video"}
        return link

    # ---------- 单元格取值 ----------
    @staticmethod
    def _is_checked(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, list):
            return len(value) > 0
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1", "checked"}
        return False

    @staticmethod
    def _as_ms(value: Any) -> int | None:
        """兼容飞书 epoch 时间戳及文本日期（如 2026-07-31）。"""
        if value is None or value == "":
            return None
        text = KolVideoTable._text(value)
        if not text:
            return None
        try:
            timestamp = int(float(text))
            return timestamp * 1000 if timestamp < 1_000_000_000_000 else timestamp
        except (TypeError, ValueError):
            pass
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
            try:
                return int(datetime.strptime(text, fmt).timestamp() * 1000)
            except ValueError:
                continue
        return None

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "".join(KolVideoTable._text(v) for v in value).strip()
        if isinstance(value, dict):
            for key in ("link", "url", "text", "name"):
                if key in value:
                    t = KolVideoTable._text(value[key])
                    if t:
                        return t
        return str(value).strip()
