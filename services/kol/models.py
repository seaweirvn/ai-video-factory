"""KOL 原始视频下载链路的数据模型（自 seaweir-video 迁入）。

独立于 AI 剪辑：只承载「飞书创作者表一行 -> 下载 -> 上传 OneDrive」所需的最小信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SourceVideo:
    """飞书创作者表里一条「待下载」记录。"""

    record_id: str
    source_url: str
    title: str = ""
    tiktok_id: str = ""
    is_featured: bool = False
    release_ms: int | None = None  # 视频发布时间（飞书日期字段，epoch 毫秒），用于按月份归档
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadedVideo:
    """下载到本地的一条视频 + 回写飞书所需的元数据。"""

    record_id: str
    source_url: str
    file_path: Path
    video_id: str = ""
    title: str = ""
    tiktok_id: str = ""
    is_featured: bool = False
    duration_seconds: float | None = None
    file_size_bytes: int | None = None
    release_ms: int | None = None  # 透传发布时间，供按月份归档
