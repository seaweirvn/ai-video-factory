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

from adapters.ffmpeg import concat_clips
from adapters.onedrive import OneDriveClient, get_onedrive_client
from app.config import get_settings
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


class EditService:
    def __init__(
        self,
        onedrive: OneDriveClient,
        workspace_dir: Path,
        data_dir: Path,
        render_folder: str,
    ) -> None:
        self.onedrive = onedrive
        self.renders_dir = Path(workspace_dir) / "renders"
        self.mappings_dir = Path(data_dir) / "renders"
        self.render_folder = render_folder
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
            if progress:
                progress(1.0)
            logger.info("成片完成 - {} ({}s, {} 段)", name, result.duration_sec, len(clips_meta))
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)

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
    return EditService(
        onedrive=get_onedrive_client(),
        workspace_dir=s.workspace_dir,
        data_dir=s.data_dir,
        render_folder=s.onedrive_render_folder,
    )
