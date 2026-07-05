from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key
from services.publish import get_publish_service

router = APIRouter(prefix="/publish", tags=["publish"], dependencies=[Depends(require_api_key)])


class RenderIn(BaseModel):
    name: str
    onedrive_link: str = ""
    feishu_record_id: str = ""
    caption: str = ""
    title: str = ""


class ScheduleRequest(BaseModel):
    renders: list[RenderIn]
    accounts: list[str]
    date: str | None = None          # YYYY-MM-DD，默认今天
    seed: int | None = None


class RunRequest(BaseModel):
    date: str | None = None          # 执行哪天的到期条目，默认今天


@router.post("/schedule")
async def schedule_publish(req: ScheduleRequest):
    """排期：把成片按账号分配并排出白天错峰发布时间，落地发布计划。"""
    on_date = date_cls.fromisoformat(req.date) if req.date else None
    try:
        items = get_publish_service().schedule(
            renders=[r.model_dump() for r in req.renders],
            accounts=req.accounts,
            on_date=on_date,
            seed=req.seed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "count": len(items), "data": [it.to_dict() for it in items]}


@router.post("/run")
async def run_due(req: RunRequest):
    """执行到期发布（发布器未配置第三方工具时为占位，仅记录状态）。"""
    on_date = date_cls.fromisoformat(req.date) if req.date else None
    summary = get_publish_service().run_due(on_date=on_date)
    return {"ok": True, **summary}


@router.get("/items")
async def list_items(date: str | None = None):
    """查看某天的发布计划与状态。"""
    on_date = date_cls.fromisoformat(date) if date else None
    items = get_publish_service().list_items(on_date)
    return {"ok": True, "count": len(items), "data": [it.to_dict() for it in items]}
