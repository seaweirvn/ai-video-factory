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
    material_id: str = ""
    product_model: str = ""
    roles: list[MaterialRole] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    onedrive_link: str = ""
    duration_sec: float = 0.0
    keep_original_audio: bool = False
    score: float | None = None
    enabled: bool = True

    def has_role(self, role: MaterialRole) -> bool:
        return role in self.roles


class RenderClip(BaseModel):
    """成片计划中的一个片段：某条素材以某个角色被使用。"""

    record_id: str
    material_id: str = ""
    role_used: MaterialRole
    onedrive_link: str = ""
    duration_sec: float = 0.0
    keep_original: bool = False


class RenderPlan(BaseModel):
    """选材引擎输出的一个成片计划：有序片段序列。"""

    product_model: str = ""
    clips: list[RenderClip] = Field(default_factory=list)
    target_duration_sec: float = 0.0

    @property
    def total_duration_sec(self) -> float:
        return round(sum(c.duration_sec for c in self.clips), 2)

    @property
    def material_ids(self) -> list[str]:
        return [c.material_id for c in self.clips]


class PublishTask(BaseModel):
    render_record_id: str
    account: str
    platform: Platform = Platform.tiktok
    scheduled_at: str | None = None
    title: str = ""
    caption: str = ""
    tags: list[str] = Field(default_factory=list)
