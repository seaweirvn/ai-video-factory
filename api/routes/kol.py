"""KOL 原始视频下载路由（独立链路：飞书创作者表 -> TikWM -> OneDrive -> 归档）。

与 AI 剪辑无关。批量下载耗时，走异步 job；轮询 /jobs/{id} 拿结果。
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.deps import require_api_key
from jobs import get_job_manager
from services.kol import get_kol_download_service

router = APIRouter(prefix="/kol", tags=["kol"], dependencies=[Depends(require_api_key)])


class DownloadRequest(BaseModel):
    limit: int | None = None  # 只处理前 N 条（测试/限流用）；None 全量


@router.post("/download")
async def kol_download(req: DownloadRequest):
    """从飞书创作者表读「待下载」，下载 -> 上传 OneDrive -> 回写 -> 归档。"""
    s = get_settings()
    if not s.feishu_vn_kol_video_table_id:
        raise HTTPException(status_code=422, detail="未配置 FEISHU_VN_KOL_VIDEO_TABLE_ID")

    svc = get_kol_download_service()

    def task(ctx):
        return svc.run(limit=req.limit)

    job = get_job_manager().submit("kol_download", task)
    return {"ok": True, "job_id": job.id, "kind": job.kind, "status": job.status.value}
