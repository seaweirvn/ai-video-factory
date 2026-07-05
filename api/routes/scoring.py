from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key

router = APIRouter(prefix="/scoring", tags=["scoring"], dependencies=[Depends(require_api_key)])


class RecomputeRequest(BaseModel):
    country: str = "VN"
    since: str | None = None  # 只重算此时间之后的数据


@router.post("/recompute")
async def recompute_scores(req: RecomputeRequest):
    """归因 + 评分：按角色权重把成片表现分摊到素材，批量更新素材评分（阶段 6）。"""
    raise HTTPException(status_code=501, detail="not_implemented: 阶段 6 归因评分")
