"""AI 文案/字幕/封面等的统一抽象接口（扩展点）。

阶段 0 只定义接口 + 占位实现，后续接 GPT/Whisper 时新增 provider，
业务层通过 get_content_provider() 拿到实现，无需改调用方。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache


class ContentProvider(ABC):
    @abstractmethod
    def generate_caption(self, product_model: str, tags: list[str], language: str = "vi") -> dict:
        """返回 {title, caption, tags}。"""


class NullContentProvider(ContentProvider):
    """占位实现：用模板拼装，接入真实 AI 前先跑通链路。"""

    def generate_caption(self, product_model: str, tags: list[str], language: str = "vi") -> dict:
        hashtags = " ".join(f"#{t}" for t in tags[:5])
        return {
            "title": f"{product_model} - {' '.join(tags[:2])}".strip(" -"),
            "caption": f"{product_model} {hashtags}".strip(),
            "tags": tags,
        }


@lru_cache
def get_content_provider() -> ContentProvider:
    return NullContentProvider()
