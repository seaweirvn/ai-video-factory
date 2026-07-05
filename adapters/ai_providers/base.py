"""AI 文案生成的统一抽象接口（扩展点）。

- ContentProvider：抽象接口，返回 {title, caption, tags}。
- TemplateContentProvider：免密钥模板兜底，先跑通链路。
- OpenAIContentProvider：OpenAI 兼容接口（GPT/DeepSeek/本地均可）。

业务层通过 get_content_provider() 拿实现，无需关心用的是哪个。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

from loguru import logger

from app.config import get_settings


class ContentProvider(ABC):
    @abstractmethod
    def generate_caption(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        video_type: str = "",
        role_summary: str = "",
    ) -> dict:
        """返回 {title, caption, tags}。"""


class TemplateContentProvider(ContentProvider):
    """免密钥模板兜底：用产品 + 标签拼装标题/文案/标签，接入真实 AI 前先跑通。"""

    def generate_caption(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        video_type: str = "",
        role_summary: str = "",
    ) -> dict:
        clean_tags = [t.strip() for t in tags if t and t.strip()]
        hashtags = [_to_hashtag(t) for t in clean_tags[:5]]
        base_tags = ["fyp", "viral", "tiktok"]
        for t in base_tags:
            if f"#{t}" not in hashtags:
                hashtags.append(f"#{t}")
        title = f"{product_model} {' '.join(clean_tags[:2])}".strip()
        caption = f"{product_model} {' '.join(hashtags)}".strip()
        return {"title": title, "caption": caption, "tags": [h.lstrip('#') for h in hashtags]}


def _to_hashtag(tag: str) -> str:
    slug = "".join(ch for ch in tag if ch.isalnum())
    return f"#{slug or tag}"


@lru_cache
def get_content_provider() -> ContentProvider:
    s = get_settings()
    want = (s.ai_provider or "auto").lower()
    has_key = bool(s.openai_api_key)
    if want == "null":
        logger.info("AI 文案 provider = template（强制）")
        return TemplateContentProvider()
    if want in ("auto", "openai") and has_key:
        from adapters.ai_providers.openai_provider import OpenAIContentProvider

        logger.info("AI 文案 provider = openai（model={}）", s.openai_model)
        return OpenAIContentProvider(
            api_key=s.openai_api_key,
            base_url=s.openai_base_url,
            model=s.openai_model,
        )
    if want == "openai" and not has_key:
        logger.warning("AI_PROVIDER=openai 但未配置 OPENAI_API_KEY，降级为模板")
    logger.info("AI 文案 provider = template（未配置 key）")
    return TemplateContentProvider()
