"""KOL 原始视频下载编排（独立链路，自 seaweir-video 迁入）。

流程：飞书创作者表读「待下载」-> TikWM 下载 -> 上传 OneDrive -> 回写飞书 -> 本地归档。
与 AI 剪辑/发布完全解耦：不进素材库、不生成 Storyboard，成品走独立发布。

每条独立 try：单条超时/失败只跳过本条，不中断整批。
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from loguru import logger

from adapters.feishu.client import make_feishu_client
from adapters.storage import get_storage_client
from app.config import get_settings
from services.kol.downloader import TikWMDownloader
from services.kol.feishu_table import KolVideoTable
from services.kol.models import DownloadedVideo


class KolDownloadService:
    def __init__(
        self,
        table: KolVideoTable,
        downloader: TikWMDownloader,
        onedrive,
        download_dir: Path,
        archive_dir: Path,
        onedrive_folder: str,
        timeout_sec: int,
    ) -> None:
        self.table = table
        self.downloader = downloader
        self.onedrive = onedrive
        self.download_dir = Path(download_dir)
        self.archive_dir = Path(archive_dir)
        self.onedrive_folder = onedrive_folder
        self.timeout_sec = timeout_sec

    def run(self, limit: int | None = None) -> dict:
        sources = self.table.fetch_pending()
        if limit is not None:
            sources = sources[:limit]
        logger.info("KOL 待下载 {} 条", len(sources))

        downloaded = 0
        uploaded = 0
        errors: list[str] = []
        for src in sources:
            video = self._download_one(src)
            if video is None:
                errors.append(f"{src.record_id}: 下载失败/超时")
                continue
            downloaded += 1

            onedrive_link = ""
            try:
                # 按月份归档：{国家根}/{YYYY-MM}/，月份取视频发布时间，缺失回退当前月
                target_folder = f"{self.onedrive_folder.rstrip('/')}/{self._month_of(video.release_ms)}"
                self.onedrive.ensure_folder(target_folder)
                onedrive_link = self.onedrive.upload_and_share(
                    video.file_path, target_folder=target_folder
                )
                uploaded += 1
            except Exception as exc:  # noqa: BLE001 - 上传失败跳过回写与归档
                logger.exception("OneDrive 上传失败 - record_id={}", src.record_id)
                errors.append(f"{src.record_id}: 上传失败 {exc}")
                continue

            try:
                self.table.mark_downloaded(video, onedrive_link=onedrive_link)
            except Exception as exc:  # noqa: BLE001
                logger.exception("飞书回写失败 - record_id={}", src.record_id)
                errors.append(f"{src.record_id}: 回写失败 {exc}")
                continue

            try:
                self._archive(video.file_path)
            except Exception:  # noqa: BLE001 - 归档失败不影响主流程
                logger.exception("归档失败 - record_id={} path={}", src.record_id, video.file_path)

        result = {
            "pending": len(sources),
            "downloaded": downloaded,
            "uploaded": uploaded,
            "errors": errors,
        }
        logger.info("KOL 下载完成 - {}", result)
        return result

    @staticmethod
    def _month_of(release_ms: int | None) -> str:
        """由发布时间（epoch 毫秒）得到 YYYY-MM；无则用当前月（本地时区）。"""
        if release_ms:
            try:
                return datetime.fromtimestamp(release_ms / 1000, tz=timezone.utc).strftime("%Y-%m")
            except (ValueError, OverflowError, OSError):
                pass
        return datetime.now().strftime("%Y-%m")

    def _download_one(self, src) -> DownloadedVideo | None:
        """单条下载放到独立线程，加墙钟超时，避免一条卡死整批。"""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.downloader.download, src, self.download_dir)
        try:
            return future.result(timeout=self.timeout_sec)
        except FuturesTimeoutError:
            logger.error("单条下载超时（{}s），跳过 - record_id={}", self.timeout_sec, src.record_id)
            return None
        except Exception:  # noqa: BLE001
            logger.exception("下载失败 - record_id={}", src.record_id)
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _archive(self, file_path: Path) -> Path:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        target = self.archive_dir / file_path.name
        if target.exists():
            target = self._dedupe(target)
        moved = Path(shutil.move(str(file_path), str(target)))
        logger.info("KOL 视频归档 - {} -> {}", file_path.name, moved)
        return moved

    @staticmethod
    def _dedupe(target: Path) -> Path:
        stem, suffix = target.stem, target.suffix
        for i in range(1, 1000):
            candidate = target.with_name(f"{stem}-{i}{suffix}")
            if not candidate.exists():
                return candidate
        raise FileExistsError(f"归档目录中同名文件过多: {target}")


@lru_cache
def get_kol_download_service() -> KolDownloadService:
    s = get_settings()
    app_token = s.feishu_vn_kol_app_token or s.feishu_vn_bitable_app_token
    client = make_feishu_client(app_token)
    table = KolVideoTable(client, s.feishu_vn_kol_video_table_id)
    return KolDownloadService(
        table=table,
        downloader=TikWMDownloader(),
        onedrive=get_storage_client(),
        download_dir=s.kol_download_dir,
        archive_dir=s.kol_archive_dir,
        onedrive_folder=(
            s.r2_kol_prefix
            if s.storage_provider.strip().casefold() == "r2"
            else s.onedrive_kol_folder
        ),
        timeout_sec=s.kol_download_timeout_sec,
    )
