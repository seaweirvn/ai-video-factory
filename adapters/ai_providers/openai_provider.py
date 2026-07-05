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
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text.split("\n", 1)[1] if "\n" in text else text
        data = json.loads(text)
        return {
            "title": str(data.get("title", "")).strip(),
            "caption": str(data.get("caption", "")).strip(),
            "tags": [str(t).lstrip("#").strip() for t in data.get("tags", []) if str(t).strip()],
        }
