"""成交视频结构算法：Conversion Director（Storyboard 生成）+ Subtitle Resolver（多语言字幕）。

纯新增模块，不改动阶段 0-4 现有主流程。produce 侧通过开关可选接入（默认关闭）。
"""

from __future__ import annotations

from services.storyboard.director import ConversionDirector, get_conversion_director
from services.storyboard.models import Storyboard, StageSpec, SubtitleIntent
from services.storyboard.resolver import (
    resolve_intent_list,
    resolve_storyboard,
    resolve_storyboard_script,
    resolve_subtitle,
)

__all__ = [
    "ConversionDirector",
    "get_conversion_director",
    "Storyboard",
    "StageSpec",
    "SubtitleIntent",
    "resolve_subtitle",
    "resolve_intent_list",
    "resolve_storyboard",
    "resolve_storyboard_script",
]
