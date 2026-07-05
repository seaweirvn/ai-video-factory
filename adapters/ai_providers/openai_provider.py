"""OpenAI 兼容的文案 provider（chat/completions），支持 GPT/DeepSeek/本地端点。

只用 httpx 直接打 /chat/completions，避免绑定特定 SDK，方便切换端点。
输出强制 JSON：{title, caption, tags}。
"""

from __future__ import annotations

import json

import httpx
from loguru import logger

from adapters.ai_providers.base import ContentProvider, TemplateContentProvider

_LANG_NAME = {
    "vi": "Vietnamese",
    "th": "Thai",
    "ms": "Malay",
    "id": "Indonesian",
    "en": "English",
    "zh": "Chinese",
}

_SYSTEM = (
    "You are a senior TikTok short-video copywriter for e-commerce. "
    "Write native, catchy, platform-native copy that drives views and conversions. "
    "Always answer with a strict JSON object only."
)


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    return text.strip()


class OpenAIContentProvider(ContentProvider):
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 40.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._fallback = TemplateContentProvider()

    def generate_caption(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        video_type: str = "",
        role_summary: str = "",
    ) -> dict:
        lang = _LANG_NAME.get(language.lower(), language)
        prompt = self._build_prompt(product_model, tags, lang, video_type, role_summary)
        try:
            content = self._chat(prompt)
            data = self._parse(content)
            data.setdefault("tags", tags)
            return data
        except Exception as exc:
            logger.warning("OpenAI 文案生成失败，降级模板 - {}", exc)
            return self._fallback.generate_caption(
                product_model, tags, language, video_type=video_type, role_summary=role_summary
            )

    def generate_script(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        target_sec: float = 25.0,
    ) -> list[str]:
        lang = _LANG_NAME.get(language.lower(), language)
        n = max(2, min(8, round(target_sec / 4)))  # 约每句 4 秒
        prompt = (
            f"Write a spoken TikTok voiceover script in {lang} for product '{product_model}'.\n"
            f"Target length about {int(target_sec)} seconds, ~{n} short sentences.\n"
            f"Keywords: {', '.join(tags) or 'none'}.\n"
            "Structure: a strong hook, 1-2 value/proof points, a clear call to action.\n"
            "Sentences must be short and natural to read aloud.\n"
            'Return ONLY JSON: {"sentences": ["...", "..."]} '
            f"with each sentence in {lang}."
        )
        try:
            content = self._chat(prompt)
            data = json.loads(_strip_fences(content))
            sentences = [str(s).strip() for s in data.get("sentences", []) if str(s).strip()]
            if sentences:
                return sentences
        except Exception as exc:
            logger.warning("OpenAI 口播脚本生成失败，降级模板 - {}", exc)
        return self._fallback.generate_script(product_model, tags, language, target_sec=target_sec)

    def _build_prompt(
        self, product_model: str, tags: list[str], lang: str, video_type: str, role_summary: str
    ) -> str:
        return (
            f"Write TikTok copy in {lang} for product model '{product_model}'.\n"
            f"Video type: {video_type or 'product showcase'}.\n"
            f"Video structure: {role_summary or 'HOOK + VALUE/PROOF + CTA'}.\n"
            f"Relevant tags/keywords: {', '.join(tags) or 'none'}.\n\n"
            "Return ONLY a JSON object with keys:\n"
            '  "title": a short punchy title (<= 40 chars),\n'
            '  "caption": 1-2 sentence caption ending with 4-8 relevant hashtags,\n'
            '  "tags": array of 5-10 hashtag strings without the # sign.\n'
            f"All text must be in {lang}."
        )

    def _chat(self, prompt: str) -> str:
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.9,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _parse(content: str) -> dict:
        data = json.loads(_strip_fences(content))
        return {
            "title": str(data.get("title", "")).strip(),
            "caption": str(data.get("caption", "")).strip(),
            "tags": [str(t).lstrip("#").strip() for t in data.get("tags", []) if str(t).strip()],
        }
