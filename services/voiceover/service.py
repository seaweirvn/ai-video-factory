"""配音服务：主播口语脚本 -> 逐句 TTS -> 变调/停顿 -> 拼接 + 句级字幕(SRT)。

真人感来源（重点，非随机）：
- 文案是越南主播口语（ContentProvider.generate_spoken_script）。
- 一句一义，句末标点/语义决定停顿（segment.pause_ms）。
- 重点词重读（segment.emphasis）、句型语调（segment.kind）。
- 每句「细微」随机语速/音高，只作润色。

产出 VoiceoverAsset（形状不变）：配音主音轨 + SRT + 总时长，交给剪辑服务。
"""

from __future__ import annotations

import random
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from loguru import logger

from adapters.ai_providers import (
    ContentProvider,
    ScriptSegment,
    VoiceProvider,
    audio_duration,
    get_content_provider,
    get_voice_provider,
    profile_for_country,
    resolve_style,
)
from adapters.ffmpeg import apply_speed_pitch, concat_audio, make_silence
from app.config import get_settings
from services.speech import format_for_tts
from services.storyboard.models import Storyboard


@dataclass
class VoiceoverAsset:
    audio_path: str
    srt_path: str
    total_duration: float
    language: str
    script: list[str] = field(default_factory=list)


@dataclass
class StageVoice:
    """一个 stage 的配音：音轨(可为空) + 该段应占时长 + 段内各句时间。"""

    stage: str
    audio_path: str          # 该段配音 wav（多句已拼接）；无口播时为空串
    duration: float          # 该段应占时长（= max(时间槽, 口播总长)）
    line_count: int = 0


@dataclass
class StoryboardVoice:
    """Storyboard 逐段配音产出：交给 EditService.render_storyboard 做时间槽合成。"""

    stages: list[StageVoice]
    srt_path: str            # 绝对时间轴字幕（跨段累计）
    total_duration: float
    language: str
    script: list[str] = field(default_factory=list)


@dataclass
class _Line:
    text: str
    audio: Path
    duration: float
    pause_ms: int


class VoiceoverService:
    def __init__(
        self,
        content: ContentProvider,
        voice: VoiceProvider | None,
        workspace_dir: Path,
        language: str,
        profile: str,
        emotion: str,
        speed_min: float,
        speed_max: float,
        pitch_random: int,
        pause_random: int,
    ) -> None:
        self.content = content
        self.voice = voice
        self.workspace_dir = Path(workspace_dir) / "voiceover"
        self.language = language
        self.profile = profile
        self.emotion = emotion
        self.speed_min = speed_min
        self.speed_max = speed_max
        self.pitch_random = pitch_random
        self.pause_random = pause_random

    @property
    def available(self) -> bool:
        return self.voice is not None

    def build(
        self,
        product_model: str,
        tags: list[str],
        target_sec: float,
        language: str | None = None,
        voice: str | None = None,      # 兼容旧签名：这里当作 profile 覆盖（如 vn_female_02）
        name: str = "vo",
        emotion: str | None = None,
        segments: list[ScriptSegment] | None = None,  # 外部已生成的口播 segments（接地文案）
    ) -> VoiceoverAsset:
        if self.voice is None:
            raise RuntimeError("配音需要可用的 VoiceProvider（缺 key 或依赖）")
        lang = language or self.language
        profile = voice or self.profile
        mood = emotion or self.emotion
        work = self.workspace_dir / name
        segs_dir = work / "segs"
        segs_dir.mkdir(parents=True, exist_ok=True)

        if segments is None:
            segments = self.content.generate_spoken_script(
                product_model, tags, lang, target_sec=target_sec, emotion=mood
            )
        if not segments:
            raise RuntimeError("未能生成口播脚本")

        rng = random.Random()
        lines: list[_Line] = []
        for i, seg in enumerate(segments):
            seg_mood = seg.emotion or mood  # 逐句情绪优先，回落整片情绪
            style = resolve_style(
                profile, seg_mood, seg.kind, rng=rng,
                speed_min=self.speed_min, speed_max=self.speed_max,
                pitch_random=self.pitch_random,
            )
            raw = self.voice.synthesize(
                format_for_tts(seg.text, lang), segs_dir / f"seg_{i:03d}.wav",
                profile=profile, speed=style.speed,
                style_hint=style.style_hint, emphasis=seg.emphasis,
            )
            # 原生变速的 Provider 已在合成时变速；否则由 ffmpeg 补变速。
            tempo = 1.0 if self.voice.native_speed else style.speed
            processed = apply_speed_pitch(
                raw, segs_dir / f"seg_{i:03d}_p.wav", tempo=tempo, pitch_pct=style.pitch_pct
            )
            dur = audio_duration(processed)
            pause = self._jitter_pause(seg.pause_ms, rng)
            if i == len(segments) - 1:
                pause = 0  # 末句后不加停顿
            lines.append(_Line(text=seg.text, audio=processed, duration=dur, pause_ms=pause))

        # 拼接顺序：句0 -> 停顿0 -> 句1 -> 停顿1 -> ...（末句无停顿）
        order: list[Path] = []
        for i, ln in enumerate(lines):
            order.append(ln.audio)
            if ln.pause_ms > 0:
                order.append(make_silence(ln.pause_ms, segs_dir / f"sil_{i:03d}.wav"))

        audio_path = work / "voiceover.wav"
        concat_audio(order, audio_path)
        total = round(sum(ln.duration + ln.pause_ms / 1000.0 for ln in lines), 3)

        srt_path = work / "voiceover.srt"
        srt_path.write_text(_build_srt(lines), encoding="utf-8")

        logger.info(
            "配音就绪 - provider={} profile={} mood={} {}句 {:.1f}s",
            self.voice.name, profile, mood, len(lines), total,
        )
        return VoiceoverAsset(
            audio_path=str(audio_path.resolve()),
            srt_path=str(srt_path.resolve()),
            total_duration=total,
            language=lang,
            script=[ln.text for ln in lines],
        )

    def build_storyboard(
        self,
        storyboard: Storyboard,
        name: str = "sb",
        language: str | None = None,
        voice: str | None = None,
        clip_durations: dict[str, float] | None = None,
        video_min_sec: float = 0.0,
        video_max_sec: float = 0.0,
    ) -> StoryboardVoice:
        """按 Storyboard 逐段合成配音，返回每段音轨 + 绝对时间轴 SRT。

        自由时长（不再拉齐到固定时间槽）：
        - 每段时长由「口播实际长度 + 尾部留白」驱动；无口播时用该段镜头的自然时长兜底。
        - 再 clamp 到该段建议区间 [min_sec, max_sec]（Director 给的软建议；口播更长时不截断口播）。
        - 最终总时长若低于 video_min_sec，则把不足补到最后一段（延长收尾，不打乱字幕时轴）。
        """
        if self.voice is None:
            raise RuntimeError("配音需要可用的 VoiceProvider（缺 key 或依赖）")
        s = get_settings()
        beat_min = float(getattr(s, "director_beat_min_sec", 1.5))
        beat_tail = float(getattr(s, "director_beat_tail_sec", 0.4))
        beat_max_default = float(getattr(s, "director_beat_max_sec", 0.0))
        clip_durations = clip_durations or {}

        lang = language or self.language
        profile = voice or self.profile
        work = self.workspace_dir / name
        segs_dir = work / "segs"
        segs_dir.mkdir(parents=True, exist_ok=True)

        # ---- Pass 1：逐段合成口播，测各段口播长度/镜头长度，先各自定初始时长（trim-only）----
        @dataclass
        class _Stg:
            stage: str
            wavs: list[Path]
            line_durs: list[float]
            texts: list[str]
            voice_len: float
            clip_len: float
            lo: float
            hi: float
            dur: float = 0.0

        stgs: list[_Stg] = []
        script: list[str] = []
        for si, stage in enumerate(storyboard.structure):
            # 字幕即口播：字幕与语音是同一句话（resolved_subtitles），只把品牌/型号/单位的
            # 读音交给 Speech Formatter 本地化（S2→Ét Hai）。resolved_tts 仅为历史兼容的可选覆盖。
            subs = stage.resolved_subtitles or []
            ttss = stage.resolved_tts or []
            lines: list[str] = []          # 字幕文本（SRT/显示）
            tts_lines: list[str] = []      # 送 TTS 的文本（已格式化）
            last = None
            for i, ln in enumerate(subs):
                cap = (ln or "").strip()
                if not cap or cap == last:
                    continue
                spoken = (ttss[i].strip() if i < len(ttss) and ttss[i] else "") or cap
                lines.append(cap)
                tts_lines.append(format_for_tts(spoken, lang))
                last = cap

            wavs: list[Path] = []
            line_durs: list[float] = []
            voice_len = 0.0
            for li, (text, spoken) in enumerate(zip(lines, tts_lines)):
                raw = self.voice.synthesize(spoken, segs_dir / f"s{si}_l{li}.wav", profile=profile)
                d = audio_duration(raw)
                wavs.append(raw)
                line_durs.append(d)
                voice_len += d
                script.append(text)

            clip_len = float(clip_durations.get(stage.stage, 0.0) or 0.0)
            lo = float(stage.min_sec or 0.0) or beat_min
            hi = float(stage.max_sec or 0.0) or beat_max_default

            # 初始时长：口播+留白（无口播则用镜头自然长度）；在 [lo,hi] 与镜头真实长度内取值。
            # 但**口播完整优先**：镜头/上限比口播短时，slot 兜底到「口播+尾白」，画面不足
            # 由合成端循环补足 —— 绝不因镜头短就把正在说的话切断（修复「话没说完就切镜头」）。
            speech_floor = (voice_len + beat_tail) if voice_len > 0 else 0.0
            want = (voice_len + beat_tail) if voice_len > 0 else (clip_len or lo)
            if hi > 0:
                want = min(want, hi)
            want = max(want, min(lo, clip_len or lo))
            if clip_len > 0:
                want = min(want, clip_len)
            if speech_floor > 0:
                want = max(want, speech_floor)
            stgs.append(_Stg(stage.stage, wavs, line_durs, lines, voice_len, clip_len,
                             lo, hi, round(max(0.5, want), 3)))

        # ---- 全片 [min,max] 兜底：只在「镜头未播完的余量」里增减，不引入循环/定格 ----
        self._fit_total(stgs, video_min_sec, video_max_sec)

        # ---- Pass 2：按最终时长排绝对时轴 SRT + 拼各段音轨 ----
        stage_voices: list[StageVoice] = []
        srt_entries: list[tuple[float, float, str]] = []
        cursor_abs = 0.0
        for si, st in enumerate(stgs):
            off = 0.0
            for text, d in zip(st.texts, st.line_durs):
                srt_entries.append((cursor_abs + off, cursor_abs + off + d, text))
                off += d
            stage_audio = ""
            if st.wavs:
                stage_wav = work / f"stage_{si}.wav"
                if len(st.wavs) == 1:
                    shutil.copyfile(st.wavs[0], stage_wav)
                else:
                    concat_audio(st.wavs, stage_wav)
                stage_audio = str(stage_wav.resolve())
            stage_voices.append(
                StageVoice(stage=st.stage, audio_path=stage_audio,
                           duration=st.dur, line_count=len(st.texts))
            )
            cursor_abs += st.dur

        srt_path = work / "voiceover.srt"
        srt_path.write_text(_build_srt_abs(srt_entries), encoding="utf-8")
        logger.info(
            "Storyboard 配音就绪 - provider={} profile={} {}段 {:.1f}s（trim-only 自由时长）",
            self.voice.name, profile, len(stage_voices), cursor_abs,
        )
        return StoryboardVoice(
            stages=stage_voices,
            srt_path=str(srt_path.resolve()),
            total_duration=round(cursor_abs, 3),
            language=lang,
            script=script,
        )

    @staticmethod
    def _fit_total(stgs, video_min_sec: float, video_max_sec: float) -> None:
        """把全片总时长收敛到 [min,max]，全程 trim-only（不循环/不定格/不慢放）：
        - 不足：把还有「未播完镜头余量」的段往其 clip_len 延长（多展示真实画面），按余量比例分配。
        - 超出：先挤掉各段「口播之外的留白」，仍超则等比压缩（裁画面/口播，绝不拉长）。
        """
        total = sum(s.dur for s in stgs)
        if not stgs:
            return
        if video_min_sec > 0 and total < video_min_sec:
            deficit = video_min_sec - total
            slack = [(s, max(0.0, (s.clip_len or s.dur) - s.dur)) for s in stgs]
            avail = sum(x for _, x in slack)
            if avail > 0:
                for s, sl in slack:
                    if sl <= 0:
                        continue
                    s.dur = round(s.dur + deficit * (sl / avail), 3)
            else:
                logger.warning(
                    "Storyboard 总时长 {:.1f}s 低于下限 {:.0f}s，且镜头已全部播完（trim-only 不循环兜底）",
                    total, video_min_sec,
                )
        elif video_max_sec > 0 and total > video_max_sec:
            # 1) 先把「口播之外的留白」挤掉（每段至少留口播长度）
            for s in stgs:
                floor = max(0.5, s.voice_len)
                if s.dur > floor:
                    s.dur = round(max(floor, s.dur - (total - video_max_sec)), 3)
                    total = sum(x.dur for x in stgs)
                    if total <= video_max_sec:
                        break
            # 2) 仍超则只压「口播之外的余量」；口播时长是硬底线，绝不为压时长切断口播。
            #    连口播底线都超上限的极端情况：保口播完整，接受略超 max（宁可长一点也不切话）。
            total = sum(s.dur for s in stgs)
            if total > video_max_sec:
                floors = [max(0.5, s.voice_len) for s in stgs]
                fixed = sum(floors)
                extra = total - fixed
                budget = video_max_sec - fixed
                if extra > 0 and budget > 0:
                    k = budget / extra
                    for s, fl in zip(stgs, floors):
                        s.dur = round(fl + (s.dur - fl) * k, 3)
                else:
                    for s, fl in zip(stgs, floors):
                        s.dur = fl

    def _jitter_pause(self, pause_ms: int, rng: random.Random) -> int:
        if self.pause_random <= 0:
            return max(0, pause_ms)
        return max(0, pause_ms + rng.randint(-self.pause_random, self.pause_random))


def _build_srt(lines: list[_Line]) -> str:
    out: list[str] = []
    cursor = 0.0
    for i, ln in enumerate(lines, start=1):
        start, end = cursor, cursor + ln.duration
        cursor = end + ln.pause_ms / 1000.0  # 字幕不覆盖停顿
        out.append(str(i))
        out.append(f"{_fmt_ts(start)} --> {_fmt_ts(end)}")
        out.append(ln.text)
        out.append("")
    return "\n".join(out)


def _build_srt_abs(entries: list[tuple[float, float, str]]) -> str:
    """按绝对时间轴 (start, end, text) 生成 SRT。"""
    out: list[str] = []
    for i, (start, end, text) in enumerate(entries, start=1):
        out.append(str(i))
        out.append(f"{_fmt_ts(start)} --> {_fmt_ts(end)}")
        out.append(text)
        out.append("")
    return "\n".join(out)


def _fmt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@lru_cache
def get_voiceover_service() -> VoiceoverService:
    s = get_settings()
    # 音色按国家走（不同国家用不同声音）；VOICE_PROFILE 非空时作为显式覆盖。
    profile = (s.voice_profile or "").strip() or profile_for_country(s.content_country)
    return VoiceoverService(
        content=get_content_provider(),
        voice=get_voice_provider(),
        workspace_dir=s.workspace_dir,
        language=s.content_language,
        profile=profile,
        emotion=s.voice_style,
        speed_min=s.voice_speed_min,
        speed_max=s.voice_speed_max,
        pitch_random=s.voice_pitch_random,
        pause_random=s.voice_pause_random,
    )
