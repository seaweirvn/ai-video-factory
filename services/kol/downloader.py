"""TikWM 下载器（自 seaweir-video 迁入，逻辑保持一致，仅去掉旧包依赖）。

通过 TikWM 公共接口拿到无水印地址，流式落盘；带限速重试与分块读超时，
避免单条卡死整批。命名 {tiktok_id}-{video_id}[-GOOD].mp4。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp
from loguru import logger
from yt_dlp.networking.impersonate import ImpersonateTarget

from services.kol.models import DownloadedVideo, SourceVideo

_TIKWM_API_URL = "https://tikwm.com/api/"
_MAX_DOWNLOAD_SECONDS = 300


class TikWMDownloader:
    """通过 TikWM 下载 TikTok 视频到本地。"""

    def download(self, source: SourceVideo, target_dir: Path) -> DownloadedVideo:
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        logger.info("下载 KOL 视频 - record_id={} url={}", source.record_id, source.source_url)

        try:
            metadata = self._fetch_tikwm_metadata(source.source_url)
        except Exception as exc:  # noqa: BLE001 - TikWM 不稳定，自动切免费备用链路
            logger.warning(
                "TikWM 解析失败，切换 yt-dlp - record_id={} err={}",
                source.record_id,
                exc,
            )
            return self._download_with_ytdlp(source, target_dir)
        video_url = metadata.get("hdplay") or metadata.get("play")
        if not video_url:
            raise RuntimeError(f"TikWM 未返回可下载视频地址: {metadata}")

        video_id = self._extract_tiktok_video_id(source.source_url, metadata)
        tiktok_id = source.tiktok_id or self._extract_tiktok_user_id(source.source_url)
        file_name = self._build_file_name(tiktok_id, video_id, source.is_featured)
        file_path = target_dir / file_name
        self._download_file(str(video_url), file_path)

        size = file_path.stat().st_size if file_path.exists() else None
        return DownloadedVideo(
            record_id=source.record_id,
            source_url=source.source_url,
            file_path=file_path,
            video_id=video_id,
            title=str(metadata.get("title") or source.title),
            tiktok_id=tiktok_id,
            is_featured=source.is_featured,
            duration_seconds=self._to_float(metadata.get("duration")),
            file_size_bytes=size,
            release_ms=source.release_ms,
        )

    def _download_with_ytdlp(
        self, source: SourceVideo, target_dir: Path
    ) -> DownloadedVideo:
        """TikWM 不可用时直接通过 yt-dlp 抓取 TikTok 视频。"""
        video_id = self._find_video_id(source.source_url) or source.record_id
        tiktok_id = (
            source.tiktok_id
            or self._extract_tiktok_user_id(source.source_url)
            or "tiktok"
        )
        file_path = target_dir / self._build_file_name(
            tiktok_id, video_id, source.is_featured
        )
        if file_path.exists():
            self._unlink_existing_best_effort(file_path)
        options = {
            "format": (
                "best[ext=mp4][vcodec!=none][acodec!=none]"
                "/best[ext=mp4]/best"
            ),
            "outtmpl": str(file_path),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "socket_timeout": 45,
            "overwrites": True,
            "impersonate": ImpersonateTarget(client="chrome"),
            "sleep_interval_requests": 1,
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(source.source_url, download=True)
        if not file_path.is_file() or file_path.stat().st_size <= 0:
            raise RuntimeError(f"yt-dlp 未生成视频文件: {file_path}")
        logger.info("KOL 视频已保存(yt-dlp) - {}", file_path)
        resolved_id = str((info or {}).get("id") or video_id)
        return DownloadedVideo(
            record_id=source.record_id,
            source_url=source.source_url,
            file_path=file_path,
            video_id=resolved_id,
            title=str((info or {}).get("title") or source.title),
            tiktok_id=tiktok_id,
            is_featured=source.is_featured,
            duration_seconds=self._to_float((info or {}).get("duration")),
            file_size_bytes=file_path.stat().st_size,
            release_ms=source.release_ms,
        )

    def _fetch_tikwm_metadata(self, source_url: str) -> dict:
        last_payload: dict = {}
        for attempt in range(1, 4):
            time.sleep(1.2)
            response = requests.get(
                _TIKWM_API_URL, params={"url": source_url}, timeout=(15, 45)
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") == 0:
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise RuntimeError(f"TikWM 返回数据格式异常: {payload}")
                return data
            last_payload = payload
            if "Limit" not in str(payload.get("msg", "")):
                break
            logger.warning("TikWM 限速，等待后重试 {}/3 - {}", attempt, source_url)
            time.sleep(2.0 * attempt)
        raise RuntimeError(f"TikWM 接口失败: {last_payload}")

    def _download_file(self, video_url: str, file_path: Path) -> None:
        """流式写盘，块间带读超时；单条超 _MAX_DOWNLOAD_SECONDS 视为超时。"""
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                if file_path.exists():
                    self._unlink_existing_best_effort(file_path)
                started_at = time.monotonic()
                with requests.get(video_url, stream=True, timeout=(30, 90)) as response:
                    response.raise_for_status()
                    with file_path.open("wb") as f:
                        for chunk in response.iter_content(chunk_size=256 * 1024):
                            if time.monotonic() - started_at > _MAX_DOWNLOAD_SECONDS:
                                raise requests.Timeout(
                                    f"单个视频下载超过 {_MAX_DOWNLOAD_SECONDS} 秒"
                                )
                            if chunk:
                                f.write(chunk)
                logger.info("KOL 视频已保存 - {}", file_path)
                return
            except (
                requests.Timeout,
                requests.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                last_exc = exc
                logger.warning("视频流中断/超时，重试 {}/3 - {} - {}", attempt, file_path.name, exc)
                time.sleep(2.0 * attempt)
        raise RuntimeError(f"下载失败（已重试 3 次）: {video_url}") from last_exc

    @staticmethod
    def _unlink_existing_best_effort(file_path: Path) -> None:
        """Windows 上文件常被预览/杀软占用，退避重试删除。"""
        for wait in (0.0, 0.4, 0.8, 1.6, 2.5):
            if wait:
                time.sleep(wait)
            try:
                file_path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                continue
        raise PermissionError(f"无法删除已有文件（可能被占用）: {file_path}")

    def _extract_tiktok_video_id(self, source_url: str, metadata: dict) -> str:
        for candidate in (
            source_url,
            str(metadata.get("id") or ""),
            str(metadata.get("aweme_id") or ""),
        ):
            vid = self._find_video_id(candidate)
            if vid:
                return vid
        raise ValueError(f"无法从链接提取 TikTok 视频 ID: {source_url}")

    def _build_file_name(self, tiktok_id: str, video_id: str, is_featured: bool) -> str:
        safe_tiktok = self._sanitize(tiktok_id) or "tiktok"
        safe_video = self._sanitize(video_id)
        featured = "-GOOD" if is_featured else ""
        return f"{safe_tiktok}-{safe_video}{featured}.mp4"

    @staticmethod
    def _extract_tiktok_user_id(source_url: str) -> str:
        parsed = urlparse(source_url)
        path = parsed.path if parsed.scheme else source_url
        match = re.search(r"/@([^/]+)/video/", path)
        return match.group(1) if match else ""

    @staticmethod
    def _sanitize(value: str) -> str:
        sanitized = re.sub(r'[<>:"/\\|?*\s]+', "_", value.strip())
        return sanitized.strip("._-")

    @staticmethod
    def _find_video_id(text: str) -> str:
        parsed = urlparse(text)
        path = parsed.path if parsed.scheme else text
        match = re.search(r"/video/(\d+)", path)
        if match:
            return match.group(1)
        match = re.search(r"\b(\d{10,})\b", text)
        return match.group(1) if match else ""

    @staticmethod
    def _to_float(value: object) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
