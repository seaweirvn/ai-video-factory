from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key
from services.content import get_content_service

router = APIRouter(prefix="/content", tags=["content"], dependencies=[Depends(require_api_key)])


class CaptionRequest(BaseModel):
    product_model: str
    tags: list[str] = []
    language: str | None = None
    video_type: str = ""
    role_summary: str = ""


class RenderContentRequest(BaseModel):
    name: str                       # 成片名（= 成片表“成片ID”，用于取映射/素材标签）
    record_id: str = ""             # 成片表记录 id；给了就把文案写回该行
    language: str | None = None
    write_back: bool = True


@router.post("/caption")
async def generate_caption(req: CaptionRequest):
    """按产品 + 标签生成标题/文案/标签（AI，无 key 时模板兜底）。"""
    result = get_content_service().generate(
        req.product_model, req.tags, req.language,
        video_type=req.video_type, role_summary=req.role_summary,
    )
    return {"ok": True, "data": result}


@router.post("/render")
async def generate_render_content(req: RenderContentRequest):
    """为一条成片生成文案（聚合其素材标签），并可写回成片表的 标题/文案/标签。"""
    service = get_content_service()
    try:
        if req.write_back and req.record_id:
            data = service.generate_and_write(req.name, req.record_id, req.language)
        else:
            data = service.generate_for_render(req.name, req.language)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "data": data}
