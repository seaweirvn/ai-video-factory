from __future__ import annotations

from enum import Enum


class MaterialRole(str, Enum):
    """素材在成片中的位置角色。"""

    hook = "HOOK"
    value = "VALUE"
    proof = "PROOF"
    cta = "CTA"


class MaterialStatus(str, Enum):
    pending = "pending"          # 已填 OneDrive 链接，待处理
    analyzing = "analyzing"      # 下载/读元数据中
    ready = "ready"              # 元数据齐全，可用于选材
    disabled = "disabled"        # 人工停用
    error = "error"


class RenderStatus(str, Enum):
    planned = "planned"
    rendering = "rendering"
    rendered = "rendered"
    failed = "failed"


class PublishStatus(str, Enum):
    scheduled = "scheduled"
    publishing = "publishing"
    published = "published"
    failed = "failed"


class Platform(str, Enum):
    tiktok = "tiktok"
    shopee = "shopee"
    facebook = "facebook"
    instagram = "instagram"
    youtube = "youtube"
