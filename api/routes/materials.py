from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.deps import require_api_key
from jobs import get_job_manager
from services.ingest import get_ingest_service

router = APIRouter(prefix="/materials", tags=["materials"], dependencies=[Depends(require_api_key)])


class IngestRequest(BaseModel):
    country: str = "VN"
    limit: int | None = None


class MetadataRequest(BaseModel):
    record_id: str
    onedrive_link: str


@router.post("/ingest")
async def ingest_materials(req: IngestRequest):
    """扫描飞书素材库待处理项 -> 下载 -> ffprobe -> 回写元数据（异步 job）。"""
    service = get_ingest_service()

    def task(ctx):
        return service.run(limit=req.limit, progress=ctx.set_progress)

    job = get_job_manager().submit("materials.ingest", task)
    return {"ok": True, "job_id": job.id, "kind": job.kind, "status": job.status.value}


@router.post("/metadata")
async def read_metadata(req: MetadataRequest):
    """下载单条素材并回写视频元数据（同步返回元数据）。"""
    meta = get_ingest_service().process_link(req.record_id, req.onedrive_link)
    return {"ok": True, "data": meta}


@router.get("/fields")
async def list_fields():
    """自检：列出素材表真实字段名与类型，便于核对映射。"""
    service = get_ingest_service()
    if not service.table_id:
        return {"ok": False, "error": "未配置 FEISHU_VN_MATERIAL_TABLE_ID"}
    fields = service.feishu.get_fields(service.table_id)
    data = [
        {"name": f.get("field_name"), "ui_type": f.get("ui_type") or f.get("uiType")}
        for f in fields
    ]
    return {"ok": True, "table_id": service.table_id, "data": data}