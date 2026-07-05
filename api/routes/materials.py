from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps import require_api_key

router = APIRouter(prefix="/materials", tags=["materials"], dependencies=[Depends(require_api_key)])


class IngestRequest(BaseModel):
    country: str = "VN"
    limit: int | None = None


class MetadataRequest(BaseModel):
    record_id: str
    onedrive_link: str


@router.post("/ingest")
async def ingest_materials(req: IngestRequest):
    """扫描飞书素材库待处理项 -> 下载 -> ffprobe -> 回写元数据（阶段 1）。"""
    raise HTTPException(status_code=501, detail="not_implemented: 阶段 1 素材摄取")


@router.post("/metadata")
async def read_metadata(req: MetadataRequest):
    """下载单条素材并回写视频元数据（阶段 1）。"""
    raise HTTPException(status_code=501, detail="not_implemented: 阶段 1 元数据回写")
