"""剪辑服务：把一个 RenderPlan 渲染成一条成片。

流程：下载各片段 → ffmpeg 归一化并拼接 → 本地成片 →（可选）上传 OneDrive。
同时把“成片-素材映射”落到本地 data/renders/<name>.json，供后续归因用；
待飞书成片表 ID 就绪后，再补上写回飞书这一步（接口已预留）。
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from loguru import logger

from adapters.feishu import FeishuBitableClient, make_feishu_client
from adapters.ffmpeg import concat_clips
from adapters.onedrive import OneDriveClient, get_onedrive_client
from app.config import get_settings
from core.feishu_fields import RENDER_FIELD_TYPES, RENDER_FIELDS
from core.models import RenderPlan


@dataclass
class RenderResult:
    name: str
    product_model: str
    output_path: str
    duration_sec: float
    onedrive_link: str = ""
    clips: list[dict] = field(default_factory=list)
    mapping_path: str = ""
    feishu_record_id: str = ""


class EditService:
    def __init__(
        self,
        onedrive: OneDriveClient,
        workspace_dir: Path,
        data_dir: Path,
        render_folder: str,
        render_feishu: FeishuBitableClient | None = None,
        render_table_id: str = "",
    ) -> None:
        self.onedrive = onedrive
        self.renders_dir = Path(workspace_dir) / "renders"
        self.mappings_dir = Path(data_dir) / "renders"
        self.render_folder = render_folder
        self.render_feishu = render_feishu
        self.render_table_id = render_table_id
        self.renders_dir.mkdir(parents=True, exist_ok=True)
        self.mappings_dir.mkdir(parents=True, exist_ok=True)

    def render(
        self,
        plan: RenderPlan,
        name: str | None = None,
        upload: bool = False,
        progress=None,
    ) -> RenderResult:
        if not plan.clips:
            raise ValueError("空的成片计划，无法渲染")
        name = name or self._make_name(plan)
        work = self.renders_dir / "_tmp" / name
        work.mkdir(parents=True, exist_ok=True)
        output = self.renders_dir / f"{name}.mp4"

        try:
            local_clips: list[Path] = []
            total = len(plan.clips)
            for i, clip in enumerate(plan.clips):
                if progress:
                    progress(i / total * 0.6)
                dst = work / f"clip_{i:03d}.mp4"
                self.onedrive.download_share_link(clip.onedrive_link, dst)
                local_clips.append(dst)

            if progress:
                progress(0.65)
            concat_clips(local_clips, output, tmp_dir=work / "_concat")

            onedrive_link = ""
            if upload:
                if progress:
                    progress(0.85)
                onedrive_link = self.onedrive.upload_and_share(output, target_folder=self.render_folder)

            clips_meta = [
                {
                    "index": i,
                    "record_id": c.record_id,
                    "material_id": c.material_id,
                    "role_used": c.role_used.value,
                    "duration_sec": c.duration_sec,
                    "onedrive_link": c.onedrive_link,
                }
                for i, c in enumerate(plan.clips)
            ]
            result = RenderResult(
                name=name,
                product_model=plan.product_model,
                output_path=str(output.resolve()),
                duration_sec=plan.total_duration_sec,
                onedrive_link=onedrive_link,
                clips=clips_meta,
            )
            result.mapping_path = self._persist_mapping(result)

            if onedrive_link and self.render_feishu and self.render_table_id:
                if progress:
                    progress(0.95)
                try:
                    result.feishu_record_id = self._write_render_record(result, plan)
                except Exception:
                    logger.exception("写回飞书成片表失败（成片已生成，可稍后补写）- {}", name)

            if progress:
                progress(1.0)
            logger.info("成片完成 - {} ({}s, {} 段)", name, result.duration_sec, len(clips_meta))
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _write_render_record(self, result: RenderResult, plan: RenderPlan) -> str:
        """把成片写入飞书成片表；缺列自动创建（方便扩展到其他国家表）。"""
        f = self.render_feishu
        tid = self.render_table_id
        materials = ", ".join(f"{c.material_id}({c.role_used.value})" for c in plan.clips)
        values = {
            "render_id": result.name,
            "product_model": plan.product_model,
            "onedrive_link": result.onedrive_link,
            "duration": result.duration_sec,
            "status": "rendered",
            "materials": materials,
        }
        fields: dict = {}
        for key, val in values.items():
            name = f.ensure_field(tid, RENDER_FIELDS[key], RENDER_FIELD_TYPES[key])
            fields[name] = f.format_value(tid, name, val)
        record = f.create_record(tid, fields)
        record_id = record.get("record_id", "")
        logger.info("成片写回飞书 - {} -> record_id={}", result.name, record_id)
        return record_id

    def _persist_mapping(self, result: RenderResult) -> str:
        path = self.mappings_dir / f"{result.name}.json"
        path.write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path.resolve())

    @staticmethod
    def _make_name(plan: RenderPlan) -> str:
        prod = plan.product_model or "NA"
        return f"{prod}_{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:6]}"


@lru_cache
def get_edit_service() -> EditService:
    s = get_settings()
    render_feishu = None
    if s.feishu_vn_render_app_token and s.feishu_vn_render_table_id:
        render_feishu = make_feishu_client(s.feishu_vn_render_app_token)
    return EditService(
        onedrive=get_onedrive_client(),
        workspace_dir=s.workspace_dir,
        data_dir=s.data_dir,
        render_folder=s.onedrive_render_folder,
        render_feishu=render_feishu,
        render_table_id=s.feishu_vn_render_table_id,
    )
