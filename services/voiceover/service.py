"""配音服务：口播脚本 -> 逐句 TTS -> 拼接配音 + 句级字幕(SRT) + 总时长。

产出 VoiceoverAsset 交给剪辑服务：配音作为主音轨，SRT 用于烧字幕，
total_duration 用来驱动选材（画面按配音时长凑齐）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from loguru import logger

from adapters.ai_providers import ContentProvider, OpenAITTSProvider, get_content_provider, get_tts_provider
from adapters.ffmpeg import concat_audio
from app.config import get_settings


@dataclass
class VoiceoverAsset:
    audio_path: str
    srt_path: str
    total_duration: float
    language: str
    script: list[str] = field(default_factory=list)


class VoiceoverService:
    def __init__(
        self,
        content: ContentProvider,
        tts: OpenAITTSProvider | None,
        workspace_dir: Path,
        language: str,
        voice: str,
    ) -> None:
        self.content = content
        self.tts = tts
        self.workspace_dir = Path(workspace_dir) / "voiceover"
        self.language = language
        self.voice = voice

    @property
    def available(self) -> bool:
        return self.tts is not None

    def build(
        self,
        product_model: str,
        tags: list[str],
        target_sec: float,
        language: str | None = None,
        voice: str | None = None,
        name: str = "vo",
    ) -> VoiceoverAsset:
        if self.tts is None:
            raise RuntimeError("配音需要 OPENAI_API_KEY（TTS 未启用）")
        lang = language or self.language
        work = self.workspace_dir / name
        work.mkdir(parents=True, exist_ok=True)

        sentences = self.content.generate_script(product_model, tags, lang, target_sec=target_sec)
        segments = self.tts.synthesize_segments(sentences, work / "segs", voice or self.voice)

        audio_path = work / "voiceover.wav"
        concat_audio([s.path for s in segments], audio_path)
        total = round(sum(s.duration for s in segments), 3)

        srt_path = work / "voiceover.srt"
        srt_path.write_text(_build_srt(segments), encoding="utf-8")

        logger.info("配音就绪 - {} 句 {:.1f}s -> {}", len(segments), total, audio_path.name)
        return VoiceoverAsset(
            audio_path=str(audio_path.resolve()),
            srt_path=str(srt_path.resolve()),
            total_duration=total,
            language=lang,
            script=sentences,
        )


def _build_srt(segments) -> str:
    lines: list[str] = []
    cursor = 0.0
    for i, seg in enumerate(segments, start=1):
        start, end = cursor, cursor + seg.duration
        cursor = end
        lines.append(str(i))
        lines.append(f"{_fmt_ts(start)} --> {_fmt_ts(end)}")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


def _fmt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@lru_cache
def get_voiceover_service() -> VoiceoverService:
    s = get_settings()
    return VoiceoverService(
        content=get_content_provider(),
        tts=get_tts_provider(),
        workspace_dir=s.workspace_dir,
        language=s.content_language,
        voice=s.tts_voice,
    )
