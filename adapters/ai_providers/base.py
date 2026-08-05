"""AI 文案生成的统一抽象接口（扩展点）。

- ContentProvider：抽象接口，返回 {title, caption, tags}。
- TemplateContentProvider：免密钥模板兜底，先跑通链路。
- OpenAIContentProvider：OpenAI 兼容接口（GPT/DeepSeek/本地均可）。

业务层通过 get_content_provider() 拿实现，无需关心用的是哪个。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache

from loguru import logger

from app.config import get_settings

# 句型：影响语调（question 上扬、exclaim/cta 更用力）。与 voice.KIND_STYLES 对齐。
_VALID_KINDS = {"statement", "question", "exclaim", "cta", "call"}


@dataclass
class ScriptSegment:
    """一句口播：文本 + 停顿(ms) + 重点词 + 句型 + 情绪。逐句合成 + 停顿感知字幕用。"""
    text: str
    pause_ms: int = 200
    emphasis: list[str] = field(default_factory=list)
    kind: str = "statement"
    emotion: str = ""  # 逐句情绪覆盖（normal/happy/excited/review/live）；空则用整片默认

    @staticmethod
    def infer_pause(text: str, fallback: int = 200) -> int:
        """GPT 没给 pause 时，按句末标点/语义给个自然停顿。"""
        t = text.strip()
        if t.endswith(("!", "?", "！", "？")):
            return 350
        if t.endswith(("...", "…")):
            return 500
        if t.endswith((".", "。")):
            return 300
        if t.endswith((",", "，", "、")):
            return 150
        return fallback

    @staticmethod
    def infer_kind(text: str) -> str:
        t = text.strip()
        if t.endswith(("?", "？")):
            return "question"
        if t.endswith(("!", "！")):
            return "exclaim"
        return "statement"


@dataclass
class SceneContext:
    """一个画面片段的语义（来自素材表的一条素材，按成片顺序排列）。"""
    role: str = ""                # HOOK / VALUE / PROOF / CTA
    material_type: str = ""       # 素材类型（手持展示 / 空摇……）
    shooting_content: str = ""    # 拍摄内容（当前画面，接地最高优先级）
    main_tag: str = ""            # 主标签（一级卖点）
    aux_tags: list[str] = field(default_factory=list)  # 辅助标签（二级卖点）


@dataclass
class ContentContext:
    """GPT 文案生成的完整输入：素材语义（画面）+ 产品中心背景。

    优先级（生成时从高到低）：拍摄内容 > 主标签 > 辅助标签 > 产品定位。
    只能围绕画面已体现的内容说，不得编造画面没有的卖点。
    """
    product_model: str = ""
    product_positioning: str = ""     # 产品定位（背景，只能辅助，不抢主线）
    target_audience: str = ""         # 目标人群
    product_selling_points: list[str] = field(default_factory=list)  # 产品中心聚合卖点（背景证明池）
    forbidden_words: list[str] = field(default_factory=list)  # 禁用词（绝不能出现）
    # 主画面（默认取 HOOK；单素材场景即该素材）
    material_type: str = ""
    shooting_content: str = ""
    primary_tag: str = ""
    secondary_tags: list[str] = field(default_factory=list)
    # 整片有序画面序列（多片段成片时，让口播贴合画面顺序）
    scenes: list[SceneContext] = field(default_factory=list)
    country: str = "VN"
    language: str = "vi"
    target_sec: float = 25.0
    emotion: str = "live"

    def keyword_tags(self) -> list[str]:
        out: list[str] = []
        for t in ([self.primary_tag] if self.primary_tag else []) + self.secondary_tags:
            t = t.strip().lstrip("#")
            if t and t not in out:
                out.append(t)
        return out


@dataclass
class ContentPack:
    """一次生成同时产出：标题 + 文案 + 话题标签 + 逐句口播 segments。"""
    title: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    segments: list[ScriptSegment] = field(default_factory=list)


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

    @abstractmethod
    def generate_script(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        target_sec: float = 25.0,
    ) -> list[str]:
        """生成口播脚本，返回分句列表（用于逐句 TTS + 句级字幕）。"""

    def generate_spoken_script(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        target_sec: float = 25.0,
        emotion: str = "live",
    ) -> list[ScriptSegment]:
        """生成主播口语脚本（带停顿/重点/句型）。

        默认实现：把 generate_script 的分句包装成 segment（停顿按标点推断）。
        接了 LLM 的 provider 会重写本方法，直接产出更地道的口语 segments。
        """
        sentences = self.generate_script(
            product_model, tags, language, target_sec=target_sec
        )
        return [
            ScriptSegment(
                text=s,
                pause_ms=ScriptSegment.infer_pause(s),
                kind=ScriptSegment.infer_kind(s),
            )
            for s in sentences
        ]

    def generate_content_pack(
        self, ctx: ContentContext, *, want_segments: bool = True
    ) -> ContentPack:
        """基于素材语义 + 产品背景，一次产出 标题/文案/标签/逐句口播。

        默认实现用现有的 caption/script 方法拼装（不接地，仅兜底）；
        接了 LLM 的 provider 会重写本方法，按接地规则做到「口播贴合画面」。
        """
        tags = ctx.keyword_tags()
        cap = self.generate_caption(ctx.product_model, tags, ctx.language)
        segments: list[ScriptSegment] = []
        if want_segments:
            segments = self.generate_spoken_script(
                ctx.product_model, tags, ctx.language,
                target_sec=ctx.target_sec, emotion=ctx.emotion,
            )
        return ContentPack(
            title=cap.get("title", ""),
            caption=cap.get("caption", ""),
            hashtags=cap.get("tags", []),
            segments=segments,
        )


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

    def generate_script(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        target_sec: float = 25.0,
    ) -> list[str]:
        # 免密钥兜底：无法产出地道口播稿，用产品+标签拼几句占位（真正配音需接 AI）。
        clean_tags = [t.strip().lstrip("#") for t in tags if t and t.strip()]
        lines = [f"{product_model}"]
        if clean_tags:
            lines.append(" ".join(clean_tags[:3]))
        lines.append(product_model)
        return [ln for ln in lines if ln]


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
