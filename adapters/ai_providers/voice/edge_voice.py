"""EdgeTTS 配音 Provider（免费，越南语原生神经音色）。

vi-VN-HoaiMyNeural（女）/ vi-VN-NamMinhNeural（男）比多语种通用音色更像
越南本地人。原生支持 rate 变速；输出 mp3 后转 wav 交给下游统一处理。
需要网络访问微软 TTS 端点：pip install edge-tts。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import edge_tts  # 顶层 import：未安装时 get_voice_provider 里按 ImportError 处理
from loguru import logger

from adapters.ai_providers.voice.base import VoiceProvider, resolve_profile


class EdgeTTSVoiceProvider(VoiceProvider):
    name = "edge"
    # edge-tts 传非 0 rate（SSML prosody）会偶发 NoAudioReceived，
    # 故固定 +0% 合成，变速交给上层 ffmpeg atempo。
    native_speed = False

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
        voice = resolve_profile(profile).get("edge") or "vi-VN-HoaiMyNeural"

        mp3_path = dst.with_suffix(".edge.mp3")
        self._save_with_retry(text, voice, mp3_path)
        self._to_wav(mp3_path, dst)
        mp3_path.unlink(missing_ok=True)
        return dst

    def _save_with_retry(self, text: str, voice: str, mp3_path: Path, attempts: int = 4) -> None:
        # edge-tts 免费端点会间歇性 NoAudioReceived，重试+退避通常可恢复。
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                asyncio.run(self._save(text, voice, mp3_path))
                if mp3_path.exists() and mp3_path.stat().st_size > 0:
                    return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("edge 合成失败(第{}次)，重试 - {}", i + 1, str(exc)[:80])
            time.sleep(1.0 + i)
        raise RuntimeError(f"edge 合成多次失败: {last_exc}")

    @staticmethod
    async def _save(text: str, voice: str, mp3_path: Path) -> None:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(mp3_path))

    @staticmethod
    def _to_wav(src: Path, dst: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg，无法转码 edge 配音")
        result = subprocess.run(
            [ffmpeg, "-y", "-i", str(src), "-ar", "44100", "-ac", "2",
             "-c:a", "pcm_s16le", str(dst)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"edge mp3->wav 失败: {(result.stderr or '')[-400:]}")
