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
from adapters.ffmpeg import compose_storyboard, compose_with_voiceover, concat_clips
from adapters.storage import StorageClient, get_storage_client
from app.config import get_settings
from core.feishu_fields import RENDER_FIELD_TYPES, RENDER_FIELDS
from core.models import RenderPlan
from services.storyboard.models import Storyboard
from services.voiceover import StoryboardVoice, VoiceoverAsset


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
    voiceover: bool = False
    script: list[str] = field(default_factory=list)
    subtitle_language: str = ""


class EditService:
    def __init__(
        self,
        onedrive: StorageClient,
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
        voiceover: VoiceoverAsset | None = None,
        kept_volume: float = 0.7,
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
            if voiceover is not None:
                # 配音驱动：音轨=配音+勾选片段原声，烧字幕，裁到配音时长
                compose_clips = [
                    {"path": local_clips[i], "keep_original": plan.clips[i].keep_original}
                    for i in range(total)
                ]
                compose_with_voiceover(
                    compose_clips,
                    voiceover_audio=Path(voiceover.audio_path),
                    output=output,
                    duration=voiceover.total_duration,
                    srt=Path(voiceover.srt_path) if voiceover.srt_path else None,
                    kept_volume=kept_volume,
                    tmp_dir=work / "_vo",
                )
                out_duration = voiceover.total_duration
            else:
                concat_clips(local_clips, output, tmp_dir=work / "_concat")
                out_duration = plan.total_duration_sec

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
                    "keep_original": c.keep_original,
                    "onedrive_link": c.onedrive_link,
                }
                for i, c in enumerate(plan.clips)
            ]
            result = RenderResult(
                name=name,
                product_model=plan.product_model,
                output_path=str(output.resolve()),
                duration_sec=round(out_duration, 2),
                onedrive_link=onedrive_link,
                clips=clips_meta,
                voiceover=voiceover is not None,
                script=voiceover.script if voiceover else [],
                subtitle_language=voiceover.language if voiceover else "",
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

    def render_storyboard(
        self,
        plan: RenderPlan,
        storyboard: Storyboard,
        sb_voice: StoryboardVoice,
        name: str | None = None,
        upload: bool = False,
        progress=None,
        bgm: Path | None = None,
        bgm_volume: float = 0.12,
        kept_volume: float = 0.7,
        feishu_client: FeishuBitableClient | None = None,
        table_id: str = "",
    ) -> RenderResult:
        """按 Storyboard 时间槽渲染：每段画面铺满其时间槽，字幕/配音按绝对时轴对齐。

        与 render() 平行的新路径（storyboard_enabled 时走这里），不改动原 render()。
        feishu_client/table_id 非空时把成片写回该目标表（中文孪生版写「中国」表用）；
        否则回落默认（越南成片表）。
        """
        if not plan.clips:
            raise ValueError("空的成片计划，无法渲染")
        name = name or self._make_name(plan)
        work = self.renders_dir / "_tmp" / name
        work.mkdir(parents=True, exist_ok=True)
        output = self.renders_dir / f"{name}.mp4"

        try:
            # 1) 下载全部片段一次，建 record_id -> 本地路径
            local_by_record: dict[str, Path] = {}
            total = len(plan.clips)
            for i, clip in enumerate(plan.clips):
                if progress:
                    progress(i / total * 0.5)
                dst = work / f"clip_{i:03d}.mp4"
                self.onedrive.download_share_link(clip.onedrive_link, dst)
                local_by_record[clip.record_id] = dst
            pool = list(local_by_record.values())

            # 2) 组装每段 compose 规格（空段从片池借片，保证画面连续）
            keep_by_record = {c.record_id: bool(c.keep_original) for c in plan.clips}
            stage_specs: list[dict] = []
            pool_idx = 0
            for stv, stage in zip(sb_voice.stages, storyboard.structure):
                clip_paths = [local_by_record[r] for r in stage.clip_record_ids if r in local_by_record]
                if not clip_paths and pool:
                    clip_paths = [pool[pool_idx % len(pool)]]
                    pool_idx += 1
                # 该段勾选「保留原声」的首个源片段（借片不带原声）
                orig_src = next(
                    (local_by_record[r] for r in stage.clip_record_ids
                     if keep_by_record.get(r) and r in local_by_record),
                    None,
                )
                stage_specs.append(
                    {
                        "clips": clip_paths,
                        "duration": stv.duration,
                        "voice": Path(stv.audio_path) if stv.audio_path else None,
                        "orig_src": orig_src,
                    }
                )

            if progress:
                progress(0.6)
            compose_storyboard(
                stage_specs,
                output=output,
                srt=Path(sb_voice.srt_path) if sb_voice.srt_path else None,
                tmp_dir=work / "_sb",
                bgm=bgm,
                bgm_volume=bgm_volume,
                kept_volume=kept_volume,
            )
            out_duration = sb_voice.total_duration

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
                    "keep_original": c.keep_original,
                    "onedrive_link": c.onedrive_link,
                }
                for i, c in enumerate(plan.clips)
            ]
            result = RenderResult(
                name=name,
                product_model=plan.product_model,
                output_path=str(output.resolve()),
                duration_sec=round(out_duration, 2),
                onedrive_link=onedrive_link,
                clips=clips_meta,
                voiceover=True,
                script=sb_voice.script,
                subtitle_language=sb_voice.language,
            )
            result.mapping_path = self._persist_mapping(result)

            wb_feishu = feishu_client or self.render_feishu
            wb_table = table_id or self.render_table_id
            if onedrive_link and wb_feishu and wb_table:
                if progress:
                    progress(0.95)
                try:
                    result.feishu_record_id = self._write_render_record(
                        result, plan, feishu=wb_feishu, table_id=wb_table
                    )
                except Exception:
                    logger.exception("写回飞书成片表失败（成片已生成，可稍后补写）- {}", name)

            if progress:
                progress(1.0)
            logger.info("结构成片完成 - {} ({}s, 4 段时间槽)", name, result.duration_sec)
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _write_render_record(
        self,
        result: RenderResult,
        plan: RenderPlan,
        feishu: FeishuBitableClient | None = None,
        table_id: str = "",
    ) -> str:
        """把成片写入飞书成片表；缺列自动创建（方便扩展到其他国家表）。

        feishu/table_id 非空时写入该目标表（中文孪生版写「中国」表）；否则用默认越南表。
        """
        f = feishu or self.render_feishu
        tid = table_id or self.render_table_id
        materials = ", ".join(
            f"{c.material_id}({c.role_used.value}{'+原声' if c.keep_original else ''})"
            for c in plan.clips
        )
        values = {
            "render_id": result.name,
            "product_model": plan.product_model,
            "onedrive_link": result.onedrive_link,
            "duration": result.duration_sec,
            "status": "rendered",
            "materials": materials,
            "voiceover": result.voiceover,
            "script": "\n".join(result.script),
            "subtitle_language": result.subtitle_language,
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
        onedrive=get_storage_client(),
        workspace_dir=s.workspace_dir,
        data_dir=s.data_dir,
        render_folder=(
            s.r2_render_prefix
            if s.storage_provider.strip().casefold() == "r2"
            else s.onedrive_render_folder
        ),
        render_feishu=render_feishu,
        render_table_id=s.feishu_vn_render_table_id,
    )
