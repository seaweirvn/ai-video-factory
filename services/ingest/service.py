"""阶段 1：素材摄取 + 元数据回写。

对每条待处理素材：
1. 从素材库读到 `ONEDRIVE链接`（文本+超链接，取其真实 URL）。
2. 用 Graph 按分享链接下载到临时工作区。
3. ffprobe 读元数据（时长/分辨率/FPS/大小/音频）。
4. 回写 `单条时长`（按秒）并勾选 `读取时长` 作为完成标记。
5. 清理临时文件。

真实素材库位于“营销”知识库多维表格（app_token 与主 bitable 不同）；
表内只有时长这一元数据列，其余（分辨率/FPS/...）暂无列，仅记录到日志。
单条失败不阻塞其他素材（“下不了就跳过”），未勾选的记录下次会重试。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

from adapters.feishu import FeishuBitableClient, make_feishu_client
from adapters.ffmpeg import probe_metadata
from adapters.onedrive import OneDriveClient, get_onedrive_client
from app.config import get_settings
from core.enums import MaterialRole
from core.feishu_fields import MATERIAL_FIELDS
from core.models import VideoMetadata


class IngestService:
    def __init__(
        self,
        feishu: FeishuBitableClient,
        onedrive: OneDriveClient,
        table_id: str,
        workspace_dir: Path,
    ) -> None:
        self.feishu = feishu
        self.onedrive = onedrive
        self.table_id = table_id
        self.workspace_dir = Path(workspace_dir)

    # ---------- 对外入口 ----------
    def run(self, limit: int | None = None, progress=None) -> dict[str, Any]:
        if not self.table_id:
            raise RuntimeError("未配置素材表 ID（FEISHU_VN_MATERIAL_TABLE_ID）")
        pending = self.fetch_pending()
        if limit:
            pending = pending[:limit]
        total = len(pending)
        logger.info("待处理素材 {} 条", total)

        succeeded, failed = 0, 0
        for idx, item in enumerate(pending, start=1):
            record_id = item["record_id"]
            try:
                self._process(record_id, item["link"], item.get("material_id", ""))
                succeeded += 1
            except Exception:  # noqa: BLE001 - 单条失败跳过，未勾选下次重试
                failed += 1
                logger.exception("素材处理失败，跳过 - record_id={}", record_id)
            if progress:
                progress(idx / total if total else 1.0)

        summary = {"total": total, "succeeded": succeeded, "failed": failed}
        logger.info("素材摄取完成 - {}", summary)
        return summary

    def process_link(self, record_id: str, onedrive_link: str) -> dict[str, Any]:
        """单条：给定 record_id + 链接，下载并回写时长。"""
        return self._process(record_id, onedrive_link, "").model_dump()

    # ---------- 明细 ----------
    def fetch_pending(self) -> list[dict[str, Any]]:
        link_field = self._field("onedrive_link")
        read_field = self.feishu.resolve_field(self.table_id, MATERIAL_FIELDS["duration_read"])
        id_field = self.feishu.resolve_field(self.table_id, MATERIAL_FIELDS["material_id"])

        pending: list[dict[str, Any]] = []
        for record in self.feishu.list_records(self.table_id, text_field_as_array=True):
            fields = record.get("fields", {})
            link = self.feishu.cell_link(fields.get(link_field))
            if not link:
                continue
            # 已勾选“读取时长”视为已处理，跳过。
            if read_field and fields.get(read_field) is True:
                continue
            pending.append(
                {
                    "record_id": record.get("record_id", ""),
                    "link": link,
                    "material_id": self.feishu.cell_text(fields.get(id_field)) if id_field else "",
                }
            )
        return pending

    def _process(self, record_id: str, link: str, material_id: str) -> VideoMetadata:
        if not link:
            raise ValueError("素材缺少 OneDrive 链接")
        meta = self._probe(record_id or material_id or "tmp", link)
        self._write_metadata(record_id, meta)
        logger.info("素材完成 - id={} record_id={} 时长={}s", material_id, record_id, round(meta.duration_sec, 2))
        return meta

    def _probe(self, tag: str, link: str) -> VideoMetadata:
        """优先用 OneDrive 元数据读时长（不下载）；facet 缺失时回退到下载 + ffprobe。"""
        try:
            item = self.onedrive.get_share_item_metadata(link)
            meta = self._metadata_from_item(item)
            if meta.duration_sec > 0:
                logger.info("元数据(免下载) - {}", meta.model_dump())
                return meta
            logger.warning("OneDrive 未返回时长，回退下载+ffprobe - {}", item.get("name"))
        except Exception:
            logger.exception("读取 OneDrive 元数据失败，回退下载+ffprobe")
        return self._download_and_probe(tag, link)

    @staticmethod
    def _metadata_from_item(item: dict) -> VideoMetadata:
        video = item.get("video") or {}
        return VideoMetadata(
            duration_sec=float(video.get("duration", 0) or 0) / 1000.0,
            width=int(video.get("width", 0) or 0),
            height=int(video.get("height", 0) or 0),
            fps=round(float(video.get("frameRate", 0) or 0), 3),
            size_bytes=int(item.get("size", 0) or 0),
            has_audio=bool(video.get("audioChannels") or item.get("audio")),
            codec=str(video.get("fourCC", "") or ""),
        )

    def _download_and_probe(self, tag: str, link: str) -> VideoMetadata:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in tag if c.isalnum() or c in "-_") or "tmp"
        dest = self.workspace_dir / f"ingest_{safe}.mp4"
        try:
            self.onedrive.download_share_link(link, dest)
            meta = probe_metadata(dest)
            logger.info("元数据(ffprobe) - {}", meta.model_dump())
            return meta
        finally:
            dest.unlink(missing_ok=True)

    def _write_metadata(self, record_id: str, meta: VideoMetadata) -> None:
        fields: dict[str, Any] = {}
        # 按业务约定：时长列填“秒”（尽管列名写毫秒）。
        self._put(fields, "duration", round(meta.duration_sec, 2))
        read_field = self.feishu.resolve_field(self.table_id, MATERIAL_FIELDS["duration_read"])
        if read_field:
            fields[read_field] = True
        if not fields:
            logger.warning("素材表无可写字段，跳过回写 - record_id={}", record_id)
            return
        self.feishu.update_record(self.table_id, record_id, fields)

    def _put(self, fields: dict[str, Any], key: str, value: Any) -> None:
        name = self.feishu.resolve_field(self.table_id, MATERIAL_FIELDS[key])
        if name:
            fields[name] = self.feishu.format_value(self.table_id, name, value)

    def _field(self, key: str) -> str:
        name = self.feishu.resolve_field(self.table_id, MATERIAL_FIELDS[key])
        if not name:
            raise RuntimeError(f"素材表缺少字段: {key} (候选 {MATERIAL_FIELDS[key]})")
        return name


def parse_roles(value: str) -> list[MaterialRole]:
    """把“HOOK / CTA / PROOF”解析成角色列表（供后续选材用）。"""
    roles: list[MaterialRole] = []
    for token in str(value).replace("，", "/").replace(",", "/").split("/"):
        token = token.strip().upper()
        for role in MaterialRole:
            if role.value == token and role not in roles:
                roles.append(role)
    return roles


@lru_cache
def get_ingest_service() -> IngestService:
    s = get_settings()
    app_token = s.feishu_vn_material_app_token or s.feishu_vn_bitable_app_token
    return IngestService(
        feishu=make_feishu_client(app_token),
        onedrive=get_onedrive_client(),
        table_id=s.feishu_vn_material_table_id,
        workspace_dir=s.workspace_dir,
    )
