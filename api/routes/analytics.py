from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key

router = APIRouter(prefix="/analytics", tags=["analytics"], dependencies=[Depends(require_api_key)])


class CollectRequest(BaseModel):
    platform: str = "tiktok"
    account: str | None = None
    country: str = "VN"


@router.post("/collect")
async def collect_analytics(req: CollectRequest):
    """回收成片表现数据（播放/完播/点赞/评论/分享/成交）写回飞书（阶段 5）。"""
    raise HTTPException(status_code=501, detail="not_implemented: 阶段 5 数据回收")
