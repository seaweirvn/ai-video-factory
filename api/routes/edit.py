from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key

router = APIRouter(prefix="/edit", tags=["edit"], dependencies=[Depends(require_api_key)])


class RenderRequest(BaseModel):
    render_record_id: str
    slots: dict[str, str] = {}  # role -> material record_id
    target_duration_sec: float | None = None


@router.post("/render")
async def render_video(req: RenderRequest):
    """自动剪辑：拼接素材生成成片，长耗时走异步 job，返回 job_id（阶段 2）。"""
    raise HTTPException(status_code=501, detail="not_implemented: 阶段 2 自动剪辑")
