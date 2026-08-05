"""MiniMax（海螺）T2A v2 配音 Provider。

- 端点：POST {base_url}/v1/t2a_v2，Bearer 鉴权（T2A 合成不需要 GroupId）。
- 音色档的 `minimax` 字段填 voice_id（系统音色 / 克隆音色 moss_audio_xxx）。
- 返回音频为 hex 编码：bytes.fromhex(resp["data"]["audio"])。
- 变速/音高交给上层 ffmpeg 统一处理，保证跨 Provider 一致。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from adapters.ai_providers.voice.base import VOICE_PROFILES, VoiceProvider

_DEFAULT_VOICE = "female-tianmei"  # profile 未配 minimax voice_id 时的系统兜底音色

# MiniMax language_boost 取值（按音色档国家自适应；不写死单一语言，多国扩展直接生效）。
_COUNTRY_BOOST = {
    "VN": "Vietnamese", "CN": "Chinese", "TH": "Thai",
    "ID": "Indonesian", "MY": "Malay", "EN": "English",
}


def _resolve_voice_id(profile: str) -> str:
    """音色档 -> 档内 minimax voice_id；未知档把 profile 原样当作 voice_id（大小写敏感）。"""
    key = (profile or "").lower()
    if key in VOICE_PROFILES:
        return VOICE_PROFILES[key].get("minimax") or _DEFAULT_VOICE
    return (profile or "").strip() or _DEFAULT_VOICE


def _boost_for_profile(profile: str) -> str:
    """按音色档国家给 language_boost（如 cn_female_01→Chinese）；未知档返回空串（用实例默认）。"""
    prof = VOICE_PROFILES.get((profile or "").lower())
    if not prof:
        return ""
    return _COUNTRY_BOOST.get((prof.get("country") or "").upper(), "")


class MiniMaxVoiceProvider(VoiceProvider):
    name = "minimax"
    native_speed = False  # 变速交给上层 ffmpeg，跨 Provider 行为一致

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.minimax.io",
        model: str = "speech-2.6-hd",
        language_boost: str = "auto",
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language_boost = language_boost
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
        payload: dict = {
            "model": self.model,
            "text": text,
            "stream": False,
            "voice_setting": {"voice_id": voice_id, "speed": 1.0, "vol": 1.0, "pitch": 0},
            "audio_setting": {
                "sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1
            },
        }
        # language_boost 优先按音色档国家自适应（中文档→Chinese，越南档→Vietnamese），
        # 避免用固定 Vietnamese 去读中文导致发音错乱；未知档回落实例默认。
        boost = _boost_for_profile(profile) or self.language_boost
        if boost and boost.lower() != "auto":
            payload["language_boost"] = boost

        resp = httpx.post(
            f"{self.base_url}/v1/t2a_v2",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        base = data.get("base_resp") or {}
        if base.get("status_code", 0) not in (0, None):
            raise RuntimeError(
                f"MiniMax T2A 失败: status_code={base.get('status_code')} msg={base.get('status_msg')}"
            )
        audio_hex = (data.get("data") or {}).get("audio")
        if not audio_hex:
            raise RuntimeError(f"MiniMax T2A 未返回音频: {str(data)[:200]}")
        dst.write_bytes(bytes.fromhex(audio_hex))
        return dst
