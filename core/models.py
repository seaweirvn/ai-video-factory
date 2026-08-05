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
    material_type: str = ""       # 素材类型（手持展示 / 空摇 / 泄力……）
    shooting_content: str = ""    # 拍摄内容（当前画面内容，文案接地的最高优先级）
    roles: list[MaterialRole] = Field(default_factory=list)
    main_tag: str = ""            # 主标签（一级卖点）
    aux_tags: list[str] = Field(default_factory=list)  # 辅助标签（二级卖点）
    tags: list[str] = Field(default_factory=list)      # 主+辅合并（选材/关键词兼容用）
    onedrive_link: str = ""
    duration_sec: float = 0.0
    keep_original_audio: bool = False
    score: float | None = None
    enabled: bool = True

    def has_role(self, role: MaterialRole) -> bool:
        return role in self.roles


class ProductProfile(BaseModel):
    """产品中心里的一条产品背景信息（定位/人群/禁用词），供文案接地。"""

    product_model: str
    positioning: str = ""
    target_audience: str = ""
    forbidden_words: list[str] = Field(default_factory=list)
    selling_points: list[str] = Field(default_factory=list)


class RenderClip(BaseModel):
    """成片计划中的一个片段：某条素材以某个角色被使用。"""

    record_id: str
    material_id: str = ""
    role_used: MaterialRole
    onedrive_link: str = ""
    duration_sec: float = 0.0
    keep_original: bool = False
    beat: str = ""  # Director 路径：该片段归属的 beat 名（hook/demo/proof...）；旧路径为空


class RenderPlan(BaseModel):
    """选材引擎输出的一个成片计划：有序片段序列。"""

    product_model: str = ""
    clips: list[RenderClip] = Field(default_factory=list)
    target_duration_sec: float = 0.0
    selling_point: str = ""  # 选材决定的核心卖点（Layer 2：证据最硬者），供 director 对齐
    playbook: str = ""  # Director 路径：本计划采用的结构名（供归因/学习）；旧路径为空

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
