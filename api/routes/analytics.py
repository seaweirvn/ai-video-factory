from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.deps import require_api_key
from services.selection.performance import record_performance

router = APIRouter(prefix="/analytics", tags=["analytics"], dependencies=[Depends(require_api_key)])


class PerfRecord(BaseModel):
    """一条成片表现（发布后由飞书/平台回流）。指标为 0~1 归一值（同产品内 min-max/分位归一）。"""

    render: str = ""
    product: str = ""
    selling_point: str = ""
    material_ids: list[str] = Field(default_factory=list)
    completion: float = 0.0   # 完播率
    engagement: float = 0.0   # 互动率（赞/评/转）
    views: float = 0.0        # 播放量（归一）
    gmv: float = 0.0          # 成交额（归一）
    ctr: float = 0.0          # 商品点击率 点击/播放（归一）
    cvr: float = 0.0          # 转化率 订单/点击（归一）
    # Director 路径回流（可选）：让大脑学习「什么结构/情绪更成交」
    playbook: str = ""
    beat_order: list[str] = Field(default_factory=list)
    angle: str = ""
    emotion: str = ""
    bgm_track: str = ""       # BGM 曲目键 provider:track_id，用于按 GMV 给音乐打分复用


class CollectRequest(BaseModel):
    platform: str = "tiktok"
    country: str = "VN"
    records: list[PerfRecord] = Field(default_factory=list)


@router.post("/collect")
async def collect_analytics(req: CollectRequest):
    """回流成片表现（完播/互动/播放/成交）到本地表现库，供选材「按分利用」。

    指标已在上游归一到 0~1。写入后选材下次自动加载：高成交的卖点/镜头会被加倍复用。
    """
    for r in req.records:
        record_performance(
            render=r.render, product=r.product, selling_point=r.selling_point,
            material_ids=r.material_ids,
            completion=r.completion, engagement=r.engagement, views=r.views, gmv=r.gmv,
            ctr=r.ctr, cvr=r.cvr,
            playbook=r.playbook, beat_order=r.beat_order, angle=r.angle, emotion=r.emotion,
            bgm_track=r.bgm_track,
        )
    return {"ingested": len(req.records), "platform": req.platform, "country": req.country}
