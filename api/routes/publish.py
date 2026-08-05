from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key
from services.publish import get_matrix_publisher, get_publish_service

router = APIRouter(prefix="/publish", tags=["publish"], dependencies=[Depends(require_api_key)])


class RenderIn(BaseModel):
    name: str
    onedrive_link: str = ""
    feishu_record_id: str = ""
    caption: str = ""
    title: str = ""
    product_model: str = ""          # 型号（S5/Z2…）；不传则从 name 前缀推断


class ScheduleRequest(BaseModel):
    renders: list[RenderIn]
    accounts: list[str]
    date: str | None = None          # YYYY-MM-DD，默认今天
    seed: int | None = None


class AutoScheduleRequest(BaseModel):
    accounts: list[str] | None = None  # 不传则用配置 PUBLISH_ACCOUNTS
    date: str | None = None            # YYYY-MM-DD，默认今天
    seed: int | None = None
    mark_scheduled: bool = True        # 排期后把成片状态置为 scheduled


class RunRequest(BaseModel):
    date: str | None = None          # 执行哪天的到期条目，默认今天


class MatrixRequest(BaseModel):
    # 不传 renders 则自动读成片表里 status=rendered 的成片
    renders: list[RenderIn] | None = None
    dry_run: bool = False            # 只演算路由不真发
    mark_published: bool = True      # 真发后把成片状态置为 published（仅自动取数时生效）


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


@router.post("/schedule/auto")
async def schedule_auto(req: AutoScheduleRequest):
    """自动排期：读成片表里 status=rendered 的成片，按配置账号排期并置为 scheduled。

    n8n 每天生产之后调一次即可，无需传成片列表。
    """
    on_date = date_cls.fromisoformat(req.date) if req.date else None
    try:
        summary = get_publish_service().schedule_from_render_table(
            accounts=req.accounts,
            on_date=on_date,
            seed=req.seed,
            mark_scheduled=req.mark_scheduled,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, **summary}


@router.post("/run")
async def run_due(req: RunRequest):
    """执行到期发布（发布器未配置第三方工具时为占位，仅记录状态）。"""
    on_date = date_cls.fromisoformat(req.date) if req.date else None
    summary = get_publish_service().run_due(on_date=on_date)
    return {"ok": True, **summary}


@router.post("/matrix")
async def publish_matrix(req: MatrixRequest):
    """一键批量双发：按型号路由到窗口（VN1/VN2/TH1…），每窗 TikTok+Shopee 各发一次并挂商品。

    - 传 renders：按给定成片发。
    - 不传 renders：自动读成片表 status=rendered 的成片。
    - dry_run=true：只返回每条会路由到哪个窗口/平台，不真发。
    """
    svc = get_publish_service()
    if req.renders:
        renders = [r.model_dump() for r in req.renders]
        source = "request"
    else:
        try:
            renders = svc._read_rendered()  # noqa: SLF001 - 复用成片表读取
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"读取成片表失败: {exc}") from exc
        source = "render_table"

    if not renders:
        return {"ok": True, "source": source, "renders": 0, "published": 0,
                "failed": 0, "skipped": 0, "details": []}

    summary = get_matrix_publisher().publish_batch(renders, dry_run=req.dry_run)

    # 自动取数 + 真发 + 有成功：把真正发出去的成片标记 published，避免重复
    if source == "render_table" and not req.dry_run and req.mark_published:
        done = [
            d for d in summary.get("details", [])
            if d.get("published", 0) > 0 and not d.get("skipped")
        ]
        done_names = {d.get("render") for d in done}
        to_mark = [r for r in renders if r.get("name") in done_names]
        if to_mark:
            try:
                svc._mark_published(to_mark)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True, "source": source, **summary}


@router.get("/routing")
async def get_routing():
    """查看当前产品→窗口路由（含就绪状态），便于核对配置。"""
    routing = get_matrix_publisher().routing
    return {
        "ok": True,
        "windows": [
            {
                "id": w.id, "name": w.name, "region": w.region,
                "platforms": w.platforms, "models": w.models,
                "enabled": w.enabled, "ready": w.ready,
            }
            for w in routing.windows
        ],
    }


@router.get("/items")
async def list_items(date: str | None = None):
    """查看某天的发布计划与状态。"""
    on_date = date_cls.fromisoformat(date) if date else None
    items = get_publish_service().list_items(on_date)
    return {"ok": True, "count": len(items), "data": [it.to_dict() for it in items]}
