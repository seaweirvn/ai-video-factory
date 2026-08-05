"""Speech Formatter：CaptionService 与 MiniMax TTS 之间的一层。

把 LLM 生成的 tts_text 里的「品牌 / 型号 / 缩写 / 单位 / 术语」按各国 Speech Knowledge Base
（config/speech/<language>.yaml）替换成本地自然读音；字幕/显示永远用 caption（英文不变）。
知识库没有的词保留原样，绝不报错/中断。多语言（vi/th/id/ms/en）按 language 自动加载。
"""

from __future__ import annotations

from services.speech.formatter import format_for_tts, load_speech_kb

__all__ = ["format_for_tts", "load_speech_kb"]
