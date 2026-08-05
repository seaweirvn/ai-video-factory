"""多语言成交字幕 Prompt Library + CaptionGenerator。

同一套 Hook -> Value -> Proof -> CTA 结构，按 国家/语言/阶段 从 prompt_library/*.yaml
读取 prompt 自动生成本地化字幕，不在代码里写死任何文案。纯新增，不改动剪辑/发布流程。
"""

from __future__ import annotations

from services.caption.generator import CaptionGenerator, get_caption_generator
from services.caption.prompt import (
    INTENT_TO_STAGE,
    STAGES,
    load_prompt,
    stage_for_intent,
)

__all__ = [
    "CaptionGenerator",
    "get_caption_generator",
    "load_prompt",
    "stage_for_intent",
    "INTENT_TO_STAGE",
    "STAGES",
]
