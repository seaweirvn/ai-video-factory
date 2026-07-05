from __future__ import annotations

from pydantic import BaseModel, Field

from core.enums import MaterialRole, Platform


class VideoMetadata(BaseModel):
    """FFmpeg 读取到的视频元数据。"""

    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    size_bytes: int = 0
    has_audio: bool = False
    codec: str = ""


class Material(BaseModel):
    """素材库中的一条素材。"""

    record_id: str
    product_model: str = ""
    role: MaterialRole | None = None
    tags: list[str] = Field(default_factory=list)
    onedrive_link: str = ""
    score: float | None = None
    enabled: bool = True
    metadata: VideoMetadata | None = None


class RenderPlanItem(BaseModel):
    """选材引擎输出的一个成片计划：按角色挑选的素材序列。"""

    product_model: str = ""
    slots: dict[MaterialRole, str] = Field(default_factory=dict)  # role -> material record_id
    target_duration_sec: float | None = None


class PublishTask(BaseModel):
    render_record_id: str
    account: str
    platform: Platform = Platform.tiktok
    scheduled_at: str | None = None
    title: str = ""
    caption: str = ""
    tags: list[str] = Field(default_factory=list)
