from fastapi import APIRouter, Depends
from pydantic import BaseModel

from adapters.ai_providers import get_content_provider
from app.deps import require_api_key

router = APIRouter(prefix="/content", tags=["content"], dependencies=[Depends(require_api_key)])


class CaptionRequest(BaseModel):
    product_model: str
    tags: list[str] = []
    language: str = "vi"


@router.post("/caption")
async def generate_caption(req: CaptionRequest):
    """生成标题/文案/标签。阶段 0 用占位 provider，后续替换为 AI。"""
    result = get_content_provider().generate_caption(req.product_model, req.tags, req.language)
    return {"ok": True, "data": result}
