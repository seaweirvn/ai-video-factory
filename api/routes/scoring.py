from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.deps import require_api_key
from services.selection.performance import get_performance_store

router = APIRouter(prefix="/scoring", tags=["scoring"], dependencies=[Depends(require_api_key)])


class RecomputeRequest(BaseModel):
    country: str = "VN"
    since: str | None = None  # 预留：只重算此时间之后的数据


@router.post("/recompute")
async def recompute_scores(req: RecomputeRequest):
    """重载表现回流并返回当前聚合评分（贝叶斯收缩后的卖点/素材分）。

    选材已在每次 plan() 时惰性加载表现库，这里主要用于强制刷新缓存 + 观测当前评分分布。
    """
    get_performance_store.cache_clear()
    store = get_performance_store()
    store.load()
    sp_scores = {sp: round(store.selling_point_score(sp), 4) for sp in store._sp}
    top_mats = sorted(
        ((mid, round(store.material_score(mid), 4)) for mid in store._mat),
        key=lambda kv: kv[1], reverse=True,
    )[:20]
    return {
        "has_data": store.has_data,
        "opt_init": store.opt_init,
        "shrink_k": store.shrink_k,
        "selling_point_scores": sp_scores,
        "top_materials": top_mats,
    }
