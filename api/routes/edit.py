from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key
from core.enums import MaterialRole
from core.models import RenderClip, RenderPlan
from jobs import get_job_manager
from services.edit import get_edit_service
from services.selection import get_selection_service

router = APIRouter(prefix="/edit", tags=["edit"], dependencies=[Depends(require_api_key)])


class ClipIn(BaseModel):
    record_id: str = ""
    material_id: str = ""
    role_used: str
    onedrive_link: str
    duration_sec: float = 0.0


class RenderRequest(BaseModel):
    product_model: str = ""
    clips: list[ClipIn] = []
    target_duration_sec: float | None = None
    upload: bool = False
    name: str | None = None


def _build_plan(req: RenderRequest) -> RenderPlan:
    if req.clips:
        try:
            clips = [
                RenderClip(
                    record_id=c.record_id,
                    material_id=c.material_id,
                    role_used=MaterialRole(c.role_used.upper()),
                    onedrive_link=c.onedrive_link,
                    duration_sec=c.duration_sec,
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
    # 未给片段则按产品自动选材一条
    try:
        plans = get_selection_service().plan(
            product_model=req.product_model,
            count=1,
            target_duration_sec=req.target_duration_sec,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return plans[0]


@router.post("/render")
async def render_video(req: RenderRequest):
    """自动剪辑：下载片段 -> 归一化拼接 -> 本地成片 ->(可选)上传 OneDrive（异步 job）。"""
    plan = _build_plan(req)
    service = get_edit_service()

    def task(ctx):
        result = service.render(plan, name=req.name, upload=req.upload, progress=ctx.set_progress)
        return result.__dict__

    job = get_job_manager().submit("edit.render", task)
    return {"ok": True, "job_id": job.id, "kind": job.kind, "status": job.status.value}
