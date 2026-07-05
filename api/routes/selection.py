from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key

router = APIRouter(prefix="/selection", tags=["selection"], dependencies=[Depends(require_api_key)])


class SelectionRequest(BaseModel):
    product_model: str
    count: int = 3
    target_duration_sec: float | None = None
    country: str = "VN"


@router.post("/plan")
async def plan_renders(req: SelectionRequest):
    """选材引擎：按角色(HOOK/VALUE/PROOF/CTA)+评分+探索比例组合成片计划（阶段 2）。"""
    raise HTTPException(status_code=501, detail="not_implemented: 阶段 2 智能选材")
