"""Cartesia 配音 Provider（低延迟高拟真，需 API key）。

音色档的 cartesia 字段填 voice id 即可用；变速/音高由上层 ffmpeg 统一处理。
切到本 Provider 无需改业务代码。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from adapters.ai_providers.voice.base import VoiceProvider, resolve_profile

_DEFAULT_VOICE = ""  # 需在 VOICE_PROFILES[...]["cartesia"] 填具体 voice id
_API_VERSION = "2024-11-13"


class CartesiaVoiceProvider(VoiceProvider):
    name = "cartesia"
    native_speed = False  # 变速交给上层 ffmpeg，跨 Provider 行为一致

    def __init__(
        self, api_key: str, base_url: str, model: str = "sonic-2", timeout: float = 60.0
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
        voice_id = resolve_profile(profile).get("cartesia") or _DEFAULT_VOICE
        if not voice_id:
            raise RuntimeError("Cartesia 需要在 VOICE_PROFILES 里配置对应 voice id")
        resp = httpx.post(
            f"{self.base_url}/tts/bytes",
            headers={
                "X-API-Key": self.api_key,
                "Cartesia-Version": _API_VERSION,
                "Content-Type": "application/json",
            },
            json={
                "model_id": self.model,
                "transcript": text,
                "voice": {"mode": "id", "id": voice_id},
                "language": "vi",
                "output_format": {
                    "container": "wav",
                    "encoding": "pcm_s16le",
                    "sample_rate": 44100,
                },
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        dst.write_bytes(resp.content)
        return dst
