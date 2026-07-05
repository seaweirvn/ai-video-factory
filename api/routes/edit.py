from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import require_api_key
from core.enums import MaterialRole
from core.models import RenderClip, RenderPlan
from jobs import get_job_manager
from services.edit import get_edit_service
from services.selection import get_selection_service
from services.voiceover import get_voiceover_service

router = APIRouter(prefix="/edit", tags=["edit"], dependencies=[Depends(require_api_key)])


class ClipIn(BaseModel):
    record_id: str = ""
    material_id: str = ""
    role_used: str
    onedrive_link: str
    duration_sec: float = 0.0
    keep_original: bool = False


class VoiceoverIn(BaseModel):
    enabled: bool | None = None       # None -> 用配置默认 voiceover_enabled_default
    language: str | None = None
    voice: str | None = None


class RenderRequest(BaseModel):
    product_model: str = ""
    clips: list[ClipIn] = []
    tags: list[str] = []              # 口播脚本/文案关键词（可选）
    target_duration_sec: float | None = None
    upload: bool = False
    name: str | None = None
    voiceover: VoiceoverIn = Field(default_factory=VoiceoverIn)


def _plan_from_clips(req: RenderRequest) -> RenderPlan:
    try:
        clips = [
            RenderClip(
                record_id=c.record_id,
                material_id=c.material_id,
                role_used=MaterialRole(c.role_used.upper()),
                onedrive_link=c.onedrive_link,
                duration_sec=c.duration_sec,
                keep_original=c.keep_original,
            )
            for c in req.clips
        ]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"非法角色: {exc}") from exc
    return RenderPlan(
        product_model=req.product_model,
        clips=clips,
        target_duration_sec=req.target_duration_sec or 0.0,
    )


@router.post("/render")
async def render_video(req: RenderRequest):
    """自动剪辑（异步 job）。

    - 原声模式（默认）：选材 -> 归一化拼接 -> (可选)上传。
    - 配音模式（voiceover.enabled）：生成口播 -> TTS 配音 -> 按配音时长选材 ->
      音轨按素材「保留原声」逐段混音 + 烧字幕 -> (可选)上传。
    """
    s = get_settings()
    vo_enabled = s.voiceover_enabled_default if req.voiceover.enabled is None else req.voiceover.enabled

    edit = get_edit_service()
    selection = get_selection_service()
    vo_service = get_voiceover_service()

    if vo_enabled and not vo_service.available:
        raise HTTPException(status_code=422, detail="配音模式需要 OPENAI_API_KEY（TTS 未启用）")
    if vo_enabled and not req.product_model and not req.clips:
        raise HTTPException(status_code=422, detail="配音模式需要 product_model 或 clips")

    def task(ctx):
        if vo_enabled:
            target = req.target_duration_sec or s.selection_target_duration_sec
            ctx.set_progress(0.05)
            asset = vo_service.build(
                product_model=req.product_model,
                tags=req.tags,
                target_sec=target,
                language=req.voiceover.language,
                voice=req.voiceover.voice,
                name=req.name or "vo",
            )
            if req.clips:
                plan = _plan_from_clips(req)
            else:
                plan = selection.plan(
                    product_model=req.product_model,
                    count=1,
                    target_duration_sec=asset.total_duration + s.voiceover_tail_margin_sec,
                )[0]
            result = edit.render(
                plan, name=req.name, upload=req.upload,
                voiceover=asset, kept_volume=s.voiceover_kept_original_volume,
                progress=ctx.set_progress,
            )
        else:
            if req.clips:
                plan = _plan_from_clips(req)
            else:
                plan = selection.plan(
                    product_model=req.product_model, count=1,
                    target_duration_sec=req.target_duration_sec,
                )[0]
            result = edit.render(plan, name=req.name, upload=req.upload, progress=ctx.set_progress)
        return result.__dict__

    job = get_job_manager().submit("edit.render", task)
    return {"ok": True, "job_id": job.id, "kind": job.kind, "status": job.status.value}
