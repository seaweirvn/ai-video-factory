"""OpenAI 配音 Provider（/audio/speech，gpt-4o-mini-tts）。

原生支持 speed 与 instructions（自然语言风格指令）——把情绪、句型语调、
重点重读都塞进 instructions，让同一把音色也能念出主播的抑扬顿挫。
"""

from __future__ import annotations

from pathlib import Path

import httpx
from loguru import logger

from adapters.ai_providers.voice.base import VoiceProvider, resolve_profile


class OpenAIVoiceProvider(VoiceProvider):
    name = "openai"

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def synthesize(
        self,
        text: str,
        dst: Path,
        *,
        profile: str = "vn_female_02",
        speed: float = 1.0,
        style_hint: str = "",
        emphasis: list[str] | None = None,
    ) -> Path:
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        voice = resolve_profile(profile).get("openai") or "alloy"
        instructions = style_hint
        if emphasis:
            instructions += " Stress these words strongly: " + ", ".join(emphasis) + "."

        body = {
            "model": self.model,
            "voice": voice,
            "input": text,
            "response_format": "wav",
            "speed": round(max(0.25, min(4.0, speed)), 3),
        }
        if instructions:
            body["instructions"] = instructions

        try:
            self._post(body, dst)
        except httpx.HTTPStatusError as exc:
            # 老模型不支持 instructions/speed 时降级重试，保证链路不断。
            logger.warning("OpenAI 配音失败({})，去掉 instructions 重试", exc.response.status_code)
            body.pop("instructions", None)
            self._post(body, dst)
        return dst

    def _post(self, body: dict, dst: Path) -> None:
        resp = httpx.post(
            f"{self.base_url}/audio/speech",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        dst.write_bytes(resp.content)
