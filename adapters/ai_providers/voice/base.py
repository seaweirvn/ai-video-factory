"""可插拔配音抽象（VoiceProvider）+ 主播音色档 / 情绪 / 句型语调映射。

设计要点（真人感来源，优先级从高到低）：
1. 文案是越南主播口语（在 content provider 里用专门 prompt 产出）。
2. 一句一义、按标点/语义决定停顿（segment.pause）。
3. 重点词重读（segment.emphasis）+ 句型语调（segment.kind）。
4. 每句语速/音高做「细微」随机，只作润色，不是真人感主要来源。

业务层只依赖 VoiceProvider.synthesize(...) 与 get_voice_provider()，
切换 OpenAI / EdgeTTS / ElevenLabs / Cartesia 不需要改业务代码。
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from loguru import logger

from app.config import get_settings

# 音色档：每个国家一把声音（不同国家用不同声音）。
# 加国家 = 在这里加一档 + 在 COUNTRY_VOICE_PROFILE 里把国家指向它。
VOICE_PROFILES: dict[str, dict] = {
    "vn_female_01": {
        "gender": "female", "country": "VN",
        "openai": "shimmer", "edge": "vi-VN-HoaiMyNeural",
        "elevenlabs": "",
        "minimax": "moss_audio_01a63aa2-79f9-11f1-a3fb-6a64dd77666f",  # MiniMax 克隆音色 VN_FEMALE_01
        "cartesia": "", "pitch_bias": 0.0, "speed_bias": 0.0,
    },
    "vn_female_02": {
        "gender": "female", "country": "VN",
        "openai": "nova", "edge": "vi-VN-HoaiMyNeural",
        "elevenlabs": "4t0bxBXbn7fCBXBrRQ6L",  # 越南女声 02（VN_FEMALE_02）
        "minimax": "moss_audio_2df779b5-79cb-11f1-8fdf-22f27a8feaff",  # MiniMax 克隆音色 VN_FEMALE_02
        "cartesia": "", "pitch_bias": 0.0, "speed_bias": 0.0,
    },
    "cn_female_01": {
        "gender": "female", "country": "CN",
        "openai": "nova", "edge": "zh-CN-XiaoxiaoNeural",
        "elevenlabs": "",
        "minimax": "female-tianmei",  # MiniMax 系统中文女声（甜美带货感），无需克隆
        "cartesia": "", "pitch_bias": 0.0, "speed_bias": 0.0,
    },
}

# 国家 -> 音色档。加国家 = 在 VOICE_PROFILES 加一档 + 在此登记。
COUNTRY_VOICE_PROFILE: dict[str, str] = {
    "VN": "vn_female_02",
    "CN": "cn_female_01",
}

# 国家 -> 口播语言名（style_hint 用；不写死越南语，便于多国扩展）。
COUNTRY_SPEAK_LANG: dict[str, str] = {
    "VN": "Vietnamese",
    "CN": "Mandarin Chinese",
    "TH": "Thai",
    "ID": "Indonesian",
    "MY": "Malay",
    "EN": "English",
}

_DEFAULT_PROFILE = "vn_female_02"


def profile_for_country(country: str) -> str:
    """按国家取音色档；未登记的国家回落默认档并告警。"""
    key = (country or "").upper()
    prof = COUNTRY_VOICE_PROFILE.get(key)
    if not prof:
        logger.warning("国家 {} 未配置音色档，回落默认 {}", key, _DEFAULT_PROFILE)
        return _DEFAULT_PROFILE
    return prof

# 情绪 -> 风格描述。速度/音高保持 1.0/0.0，避免改变 ElevenLabs 音色本色。
EMOTION_STYLES: dict[str, dict] = {
    "normal": {"speed": 1.00, "pitch": 0.0, "desc": "calm, clear and friendly"},
    "happy": {"speed": 1.00, "pitch": 0.0, "desc": "cheerful and warm"},
    "excited": {"speed": 1.00, "pitch": 0.0, "desc": "high-energy and hyped, like a flash-sale livestream"},
    "review": {"speed": 1.00, "pitch": 0.0, "desc": "an honest, conversational reviewer you can trust"},
    "live": {"speed": 1.00, "pitch": 0.0, "desc": "a natural TikTok livestreamer, casual and engaging"},
}
_DEFAULT_EMOTION = "live"

# 句型只保留文字风格提示；不再做速度/音高微调，避免变音。
KIND_STYLES: dict[str, dict] = {
    "statement": {"speed": 1.00, "pitch": 0.0, "desc": ""},
    "question": {"speed": 1.00, "pitch": 0.0, "desc": "with a natural question intonation"},
    "exclaim": {"speed": 1.00, "pitch": 0.0, "desc": "with a natural exclamation"},
    "cta": {"speed": 1.00, "pitch": 0.0, "desc": "with a clear call-to-action"},
    "call": {"speed": 1.00, "pitch": 0.0, "desc": "greeting the viewers naturally"},
}


@dataclass
class SegmentStyle:
    """一句话最终采用的合成参数。"""
    speed: float
    pitch_pct: float
    style_hint: str


def resolve_profile(profile: str) -> dict:
    return VOICE_PROFILES.get((profile or "").lower(), VOICE_PROFILES[_DEFAULT_PROFILE])


def resolve_style(
    profile: str,
    emotion: str,
    kind: str,
    *,
    rng: random.Random,
    speed_min: float,
    speed_max: float,
    pitch_random: int,
) -> SegmentStyle:
    """把 音色档 + 情绪 + 句型 + 细微随机 合成为一句话的合成参数。"""
    prof = resolve_profile(profile)
    emo = EMOTION_STYLES.get((emotion or "").lower(), EMOTION_STYLES[_DEFAULT_EMOTION])
    kd = KIND_STYLES.get((kind or "").lower(), KIND_STYLES["statement"])

    rand_speed = rng.uniform(speed_min, speed_max) if speed_max > speed_min else speed_min
    speed = rand_speed * emo["speed"] * kd["speed"] + prof["speed_bias"]
    speed = round(max(0.5, min(2.0, speed)), 3)

    rand_pitch = rng.randint(-pitch_random, pitch_random) if pitch_random > 0 else 0
    pitch_pct = round(rand_pitch + emo["pitch"] + kd["pitch"] + prof["pitch_bias"], 2)

    gender = prof["gender"]
    speak_lang = COUNTRY_SPEAK_LANG.get((prof.get("country") or "").upper(), "Vietnamese")
    hint = f"Speak {speak_lang} like a {gender} TikTok livestreamer: {emo['desc']}"
    if kd["desc"]:
        hint += f", {kd['desc']}"
    hint += ". Sound like a real person talking to the camera, not reading a script."
    return SegmentStyle(speed=speed, pitch_pct=pitch_pct, style_hint=hint)


class VoiceProvider(ABC):
    """一句话 -> 一个音频文件（wav/mp3 均可，下游会统一重编码）。

    speed 已含情绪/句型/随机与音色偏置。native_speed=True 的 Provider（OpenAI）
    自己变速；native_speed=False（Edge/ElevenLabs/Cartesia）忽略 speed，由上层
    用 ffmpeg atempo 变速。音高一律由上层 ffmpeg 统一处理，保证跨 Provider 一致。
    """

    name = "base"
    native_speed = True

    @abstractmethod
    def synthesize(
        self,
        text: str,
        dst: Path,
        *,
        profile: str = _DEFAULT_PROFILE,
        speed: float = 1.0,
        style_hint: str = "",
        emphasis: list[str] | None = None,
    ) -> Path:
        ...


@lru_cache
def get_voice_provider() -> VoiceProvider | None:
    """按配置选配音 Provider；不可用返回 None（配音模式随之不可用）。"""
    s = get_settings()
    want = (s.voice_provider or "auto").lower()

    def _openai() -> VoiceProvider | None:
        if not s.openai_api_key:
            return None
        from adapters.ai_providers.voice.openai_voice import OpenAIVoiceProvider

        return OpenAIVoiceProvider(
            api_key=s.openai_api_key, base_url=s.openai_base_url, model=s.openai_tts_model
        )

    def _edge() -> VoiceProvider | None:
        try:
            from adapters.ai_providers.voice.edge_voice import EdgeTTSVoiceProvider
        except ImportError:
            logger.warning("edge-tts 未安装，无法使用 edge 配音（pip install edge-tts）")
            return None
        return EdgeTTSVoiceProvider()

    def _elevenlabs() -> VoiceProvider | None:
        if not s.elevenlabs_api_key:
            return None
        from adapters.ai_providers.voice.elevenlabs_voice import ElevenLabsVoiceProvider

        return ElevenLabsVoiceProvider(api_key=s.elevenlabs_api_key, base_url=s.elevenlabs_base_url)

    def _cartesia() -> VoiceProvider | None:
        if not s.cartesia_api_key:
            return None
        from adapters.ai_providers.voice.cartesia_voice import CartesiaVoiceProvider

        return CartesiaVoiceProvider(api_key=s.cartesia_api_key, base_url=s.cartesia_base_url)

    def _minimax() -> VoiceProvider | None:
        if not s.minimax_api_key:
            return None
        from adapters.ai_providers.voice.minimax_voice import MiniMaxVoiceProvider

        return MiniMaxVoiceProvider(
            api_key=s.minimax_api_key,
            base_url=s.minimax_base_url,
            model=s.minimax_model,
            language_boost=s.minimax_language_boost,
        )

    builders = {
        "openai": _openai,
        "edge": _edge,
        "elevenlabs": _elevenlabs,
        "cartesia": _cartesia,
        "minimax": _minimax,
    }
    if want in builders:
        provider = builders[want]()
        if provider is None:
            logger.warning("VOICE_PROVIDER={} 不可用（缺 key 或依赖）", want)
        else:
            logger.info("配音 Provider = {}", provider.name)
        return provider

    # auto：优先 openai（已配 key、已验证），否则 edge（免费、越南语原生）。
    provider = _openai() or _edge()
    logger.info("配音 Provider = {}（auto）", provider.name if provider else "无")
    return provider
