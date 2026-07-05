"""OpenAI TTS 适配器（/audio/speech）。

逐句合成 -> 每段 wav + 时长（ffprobe），供配音服务拼接与句级字幕定时。
未配置 key 时 get_tts_provider() 返回 None，配音模式不可用。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx
from loguru import logger

from app.config import get_settings


@dataclass
class TTSSegment:
    text: str
    path: Path
    duration: float


class OpenAITTSProvider:
    def __init__(self, api_key: str, base_url: str, model: str, voice: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice
        self.timeout = timeout

    def synthesize(self, text: str, dst: Path, voice: str | None = None) -> Path:
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        resp = httpx.post(
            f"{self.base_url}/audio/speech",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "voice": voice or self.voice,
                "input": text,
                "response_format": "wav",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        dst.write_bytes(resp.content)
        return dst

    def synthesize_segments(
        self, sentences: list[str], out_dir: Path, voice: str | None = None
    ) -> list[TTSSegment]:
        out_dir = Path(out_dir)
        segments: list[TTSSegment] = []
        for i, text in enumerate(sentences):
            path = self.synthesize(text, out_dir / f"seg_{i:03d}.wav", voice)
            duration = audio_duration(path)
            segments.append(TTSSegment(text=text, path=path, duration=duration))
        logger.info("TTS 合成 {} 段，总时长 {:.1f}s", len(segments), sum(s.duration for s in segments))
        return segments


def audio_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("未找到 ffprobe，无法测配音时长")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


@lru_cache
def get_tts_provider() -> OpenAITTSProvider | None:
    s = get_settings()
    if not s.openai_api_key:
        return None
    return OpenAITTSProvider(
        api_key=s.openai_api_key,
        base_url=s.openai_base_url,
        model=s.openai_tts_model,
        voice=s.tts_voice,
    )
