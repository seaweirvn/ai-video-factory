"""ElevenLabs 配音 Provider（多语种高拟真，需 API key）。

音色档的 elevenlabs 字段填 voice_id 即可用；变速/音高由上层 ffmpeg 统一处理，
这里只负责把文本合成为音频。切到本 Provider 无需改业务代码。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from adapters.ai_providers.voice.base import VOICE_PROFILES, VoiceProvider

_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs 预置音色，profile 未配 voice_id 时兜底


def _resolve_voice_id(profile: str) -> str:
    """已知主播档 -> 档内 elevenlabs voice_id；否则把 profile 直接当作原始 voice_id。

    voice_id 大小写敏感，未知档不做 lower()，原样透传，方便直接传自定义 voice ID。
    """
    key = (profile or "").lower()
    if key in VOICE_PROFILES:
        return VOICE_PROFILES[key].get("elevenlabs") or _DEFAULT_VOICE
    return (profile or "").strip() or _DEFAULT_VOICE


class ElevenLabsVoiceProvider(VoiceProvider):
    name = "elevenlabs"
    native_speed = False  # 变速交给上层 ffmpeg，跨 Provider 行为一致

    def __init__(
        self, api_key: str, base_url: str, model: str = "eleven_multilingual_v2", timeout: float = 60.0
    ) -> None:
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
        voice_id = _resolve_voice_id(profile)
        resp = httpx.post(
            f"{self.base_url}/text-to-speech/{voice_id}",
            headers={"xi-api-key": self.api_key, "accept": "audio/mpeg"},
            json={
                "text": text,
                "model_id": self.model,
                "voice_settings": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.5},
            },
            params={"output_format": "mp3_44100_128"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        dst.write_bytes(resp.content)
        return dst
