"""用 ffprobe 读取视频元数据。

ffprobe 缺失时抛错，由上层 job 记录失败并跳过，不影响其他素材。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from loguru import logger

from core.models import VideoMetadata


def probe_metadata(path: Path) -> VideoMetadata:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"视频不存在: {path}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("未找到 ffprobe，请安装 FFmpeg 并加入 PATH")

    cmd = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)

    video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    fmt = data.get("format", {})

    meta = VideoMetadata(
        duration_sec=float(fmt.get("duration", 0) or 0),
        width=int(video_stream.get("width", 0) or 0),
        height=int(video_stream.get("height", 0) or 0),
        fps=_parse_fps(video_stream.get("avg_frame_rate", "0/0")),
        size_bytes=int(fmt.get("size", 0) or 0),
        has_audio=has_audio,
        codec=str(video_stream.get("codec_name", "")),
    )
    logger.debug("ffprobe 元数据 - {}", meta)
    return meta


def _parse_fps(value: str) -> float:
    try:
        num, den = value.split("/")
        den_f = float(den)
        return round(float(num) / den_f, 3) if den_f else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0
