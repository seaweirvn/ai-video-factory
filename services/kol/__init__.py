"""KOL 原始视频下载归档链路（独立于 AI 剪辑/发布，自 seaweir-video 迁入）。"""

from services.kol.models import DownloadedVideo, SourceVideo
from services.kol.service import KolDownloadService, get_kol_download_service

__all__ = [
    "SourceVideo",
    "DownloadedVideo",
    "KolDownloadService",
    "get_kol_download_service",
]
