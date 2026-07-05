from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key
from services.selection import get_selection_service

router = APIRouter(prefix="/selection", tags=["selection"], dependencies=[Depends(require_api_key)])


class SelectionRequest(BaseModel):
    product_model: str
    count: int = 3
    target_duration_sec: float | None = None
    seed: int | None = None
    country: str = "VN"


@router.post("/plan")
async def plan_renders(req: SelectionRequest):
    """选材引擎：HOOK+CTA 必选，VALUE/PROOF 按目标时长凑（flexible，阶段 2）。"""
    try:
        plans = get_selection_service().plan(
            product_model=req.product_model,
            count=req.count,
            target_duration_sec=req.target_duration_sec,
            seed=req.seed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "count": len(plans), "data": [p.model_dump() for p in plans]}
