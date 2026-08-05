"""生产编排路由：一次把选材→(配音)→剪辑→上传→文案回写打包成异步 job。

n8n 每天定时对每个产品调用本接口；轮询 /jobs/{id} 拿结果。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import require_api_key
from jobs import get_job_manager
from services.pipeline import get_produce_service
from services.selection import get_selection_service
from services.voiceover import get_voiceover_service

router = APIRouter(prefix="/produce", tags=["pipeline"], dependencies=[Depends(require_api_key)])


class VoiceoverIn(BaseModel):
    enabled: bool | None = None       # None -> 用配置默认 voiceover_enabled_default
    language: str | None = None
    voice: str | None = None


class ProduceRequest(BaseModel):
    # 单产品：填 product_model；批量：填 products；两者都空 -> 自动发现所有可组片产品
    product_model: str = ""
    products: list[str] = []
    count: int = 1                    # 每个产品生产几条
    target_duration_sec: float | None = None
    upload: bool = True
    generate_content: bool = True
    tags: list[str] = []              # 仅单产品模式的口播/文案关键词
    voiceover: VoiceoverIn = Field(default_factory=VoiceoverIn)


@router.get("/products")
async def list_producible():
    """列出可组片（有 HOOK 且有 CTA）的产品型号，供 n8n 数据驱动地循环生产。"""
    products = get_selection_service().producible_products()
    return {"ok": True, "count": len(products), "data": products}


@router.post("")
async def produce(req: ProduceRequest):
    s = get_settings()
    vo_enabled = s.voiceover_enabled_default if req.voiceover.enabled is None else req.voiceover.enabled
    if vo_enabled and not get_voiceover_service().available:
        raise HTTPException(status_code=422, detail="配音模式需要 OPENAI_API_KEY（TTS 未启用）")

    svc = get_produce_service()
    single = bool(req.product_model) and not req.products

    def task(ctx):
        if single:
            return svc.produce(
                product_model=req.product_model,
                count=req.count,
                target_duration_sec=req.target_duration_sec,
                voiceover_enabled=req.voiceover.enabled,
                language=req.voiceover.language,
                voice=req.voiceover.voice,
                upload=req.upload,
                generate_content=req.generate_content,
                tags=req.tags,
                progress=ctx.set_progress,
            )
        return svc.produce_batch(
            products=req.products or None,
            count=req.count,
            target_duration_sec=req.target_duration_sec,
            voiceover_enabled=req.voiceover.enabled,
            language=req.voiceover.language,
            voice=req.voiceover.voice,
            upload=req.upload,
            generate_content=req.generate_content,
            progress=ctx.set_progress,
        )

    job = get_job_manager().submit("produce", task)
    return {"ok": True, "job_id": job.id, "kind": job.kind, "status": job.status.value}
