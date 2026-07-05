from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key

router = APIRouter(prefix="/publish", tags=["publish"], dependencies=[Depends(require_api_key)])


class PublishRequest(BaseModel):
    render_record_id: str
    account: str
    platform: str = "tiktok"
    scheduled_at: str | None = None


@router.post("")
async def publish_video(req: PublishRequest):
    """发布到指定平台账号，更新飞书发布状态（阶段 4）。"""
    raise HTTPException(status_code=501, detail="not_implemented: 阶段 4 发布")
