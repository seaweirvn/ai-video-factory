"""FFmpeg 合成：把多个片段统一规格后顺序拼接为一条成片。

策略（MVP，简单稳）：
1. 逐片归一化：缩放/补边到目标分辨率、统一帧率、统一像素/时基，
   统一音频（缺音轨则补静音），编码到临时 mp4。
2. 用 concat 分离器无损拼接归一化后的片段（规格一致，-c copy 很快）。

不做转场/字幕/BGM。归一化保证混合 H264/HEVC、有无音轨都能拼。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from loguru import logger


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("未找到 ffmpeg，请安装 FFmpeg 并加入 PATH")
    return exe


def _has_audio(path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True  # 拿不准就当有音轨，交给归一化处理
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False,
    )
    return bool(result.stdout.strip())


def normalize_clip(
    src: Path,
    dst: Path,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> Path:
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 等比缩放后居中补黑边到目标尺寸，统一 SAR/帧率/时基。
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
    )
    cmd = [_ffmpeg(), "-y"]
    has_audio = _has_audio(src)
    if has_audio:
        cmd += ["-i", str(src)]
    else:
        # 无音轨则补一条静音，保证所有片段结构一致，拼接不报错。
        cmd += ["-i", str(src), "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += ["-vf", vf, "-r", str(fps)]
    if has_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    else:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
        "-video_track_timescale", "30000",
        str(dst),
    ]
    _run(cmd, f"归一化 {src.name}")
    return dst


def concat_clips(clips: list[Path], output: Path, tmp_dir: Path | None = None) -> Path:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tmp_dir or output.parent / "_concat_tmp")
    work.mkdir(parents=True, exist_ok=True)

    normalized: list[Path] = []
    try:
        for i, clip in enumerate(clips):
            norm = normalize_clip(clip, work / f"norm_{i:03d}.mp4")
            normalized.append(norm)

        list_file = work / "concat_list.txt"
        list_file.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in normalized),
            encoding="utf-8",
        )
        cmd = [
            _ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(output),
        ]
        _run(cmd, f"拼接 {len(normalized)} 段 -> {output.name}")
        return output
    finally:
        shutil.rmtree(work, ignore_errors=True)


def compose_with_voiceover(
    clips: list[dict],
    voiceover_audio: Path,
    output: Path,
    duration: float,
    srt: Path | None = None,
    kept_volume: float = 0.7,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    tmp_dir: Path | None = None,
) -> Path:
    """配音驱动合成。

    clips: 每片 {"path": 源文件, "keep_original": bool}，按顺序拼接。
    音轨 = 配音(主) + 所有 keep_original 片段的原声(按其时间轴起点延迟、压低音量)混音；
    未勾选片段丢弃原声。烧录 srt 字幕，输出裁到 duration。
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tmp_dir or output.parent / "_vo_tmp")
    work.mkdir(parents=True, exist_ok=True)

    try:
        normalized: list[Path] = []
        starts: list[float] = []
        cursor = 0.0
        for i, clip in enumerate(clips):
            norm = normalize_clip(Path(clip["path"]), work / f"norm_{i:03d}.mp4", width, height, fps)
            normalized.append(norm)
            starts.append(cursor)
            cursor += _media_duration(norm)

        subs_name = ""
        if srt is not None:
            subs_name = "subs.srt"
            shutil.copyfile(srt, work / subs_name)

        n = len(normalized)
        vo_idx = n
        parts: list[str] = []
        # 视频：concat 后（可选）烧字幕
        parts.append("".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat];")
        if subs_name:
            style = "force_style='Alignment=2,MarginV=60,Fontsize=16,Outline=1,Shadow=0'"
            parts.append(f"[vcat]subtitles={subs_name}:{style}[v];")
        else:
            parts.append("[vcat]null[v];")
        # 音频：配音主轨 + 保留原声片段
        parts.append(f"[{vo_idx}:a]aresample=44100[vo];")
        audio_labels = ["[vo]"]
        for i, clip in enumerate(clips):
            if not clip.get("keep_original"):
                continue
            ms = int(round(starts[i] * 1000))
            parts.append(f"[{i}:a]adelay={ms}|{ms},volume={kept_volume}[a{i}];")
            audio_labels.append(f"[a{i}]")
        if len(audio_labels) == 1:
            final_audio = "[vo]"
        else:
            parts.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:normalize=0[a];")
            final_audio = "[a]"

        filter_complex = "".join(parts)
        cmd = [_ffmpeg(), "-y"]
        for norm in normalized:
            cmd += ["-i", str(norm.resolve())]
        cmd += ["-i", str(Path(voiceover_audio).resolve())]
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", final_audio,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
            "-video_track_timescale", "30000",
            str(output.resolve()),
        ]
        _run(cmd, f"配音合成 {n} 段 -> {output.name}", cwd=work)
        return output
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _concat_demux(clips: list[Path], output: Path) -> Path:
    """无损拼接已归一化、规格一致的片段（concat 分离器 + -c copy）。"""
    output = Path(output)
    list_file = output.parent / f"{output.stem}_clist.txt"
    list_file.write_text(
        "\n".join(f"file '{Path(p).resolve().as_posix()}'" for p in clips),
        encoding="utf-8",
    )
    try:
        _run(
            [_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c", "copy", str(output.resolve())],
            f"拼接 {len(clips)} 段 -> {output.name}",
        )
    finally:
        list_file.unlink(missing_ok=True)
    return output


def compose_storyboard(
    stages: list[dict],
    output: Path,
    srt: Path | None = None,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    tmp_dir: Path | None = None,
    bgm: Path | None = None,
    bgm_volume: float = 0.12,
    kept_volume: float = 0.7,
) -> Path:
    """成交结构(时间槽)驱动合成。

    stages: 每段 {
        "clips": [Path,...],       # 该段画面片段（至少 1 条）
        "duration": float,         # 该段应占时长（秒）
        "voice": Path | None,      # 该段配音 wav（不足补静音；足够则占满）
        "orig_src": Path | None,   # 勾选「保留原声」的源片段；混入该段音轨（有配音则压低到 kept_volume）
    }
    每段画面「循环/裁剪」到 duration，再顺序拼接 => 视频总长 = 各段 duration 之和；
    每段音频 = 配音(主) [+ 保留原声(压低)] 补静音到 duration 后拼接，与画面严格对齐；
    最后烧 srt(绝对时间轴) 并混 BGM。
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tmp_dir or output.parent / "_sb_tmp")
    work.mkdir(parents=True, exist_ok=True)
    try:
        stage_videos: list[Path] = []
        stage_audios: list[Path] = []
        for i, st in enumerate(stages):
            slot = max(0.5, float(st.get("duration") or 0))
            clips = [Path(c) for c in (st.get("clips") or [])]
            if not clips:
                raise ValueError(f"stage#{i} 无可用画面片段")
            normalized = [
                normalize_clip(c, work / f"s{i}_n{j:02d}.mp4", width, height, fps)
                for j, c in enumerate(clips)
            ]
            # 按片数均分时间槽（专业节奏）：每个镜头分到 slot/n 秒（短则循环补，长则裁），
            # 再顺序拼接 => 段总长精确 = slot。Hook 多片=快切；Proof 多片=均匀多角度证明。
            n = len(normalized)
            segs: list[Path] = []
            acc = 0.0
            for j, nc in enumerate(normalized):
                seg_dur = (slot - acc) if j == n - 1 else round(slot / n, 3)
                acc += seg_dur
                seg = work / f"s{i}_seg{j:02d}.mp4"
                _run(
                    [_ffmpeg(), "-y", "-stream_loop", "-1", "-i", str(nc.resolve()),
                     "-t", f"{seg_dur:.3f}", "-an", "-r", str(fps),
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                     "-video_track_timescale", "30000", str(seg.resolve())],
                    f"stage#{i} 镜头{j+1}/{n} {seg_dur:.1f}s",
                )
                segs.append(seg)
            stage_v = work / f"s{i}_slot.mp4"
            if len(segs) == 1:
                shutil.copyfile(segs[0], stage_v)
            else:
                _concat_demux(segs, stage_v)
            stage_videos.append(stage_v)
            # 音频补齐/裁剪到 slot：配音主轨（无则静音）
            stage_a = work / f"s{i}_slot.wav"
            voice = st.get("voice")
            has_voice = bool(voice and Path(voice).exists())
            base_a = work / f"s{i}_voice.wav"
            if has_voice:
                _run(
                    [_ffmpeg(), "-y", "-i", str(Path(voice).resolve()),
                     "-af", "apad", "-t", f"{slot:.3f}",
                     "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(base_a.resolve())],
                    f"stage#{i} 配音补齐 {slot:.1f}s",
                )
            else:
                make_silence(int(slot * 1000), base_a)

            # 「保留原声」：混入源片段前 slot 秒的原声（有配音则压低，无配音则原声为主）
            orig_src = st.get("orig_src")
            if orig_src and Path(orig_src).exists() and _has_audio(Path(orig_src)):
                vol = max(0.0, min(1.0, float(kept_volume))) if has_voice else 1.0
                orig_a = work / f"s{i}_orig.wav"
                _run(
                    [_ffmpeg(), "-y", "-i", str(Path(orig_src).resolve()),
                     "-af", f"volume={vol:.3f},apad", "-t", f"{slot:.3f}",
                     "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(orig_a.resolve())],
                    f"stage#{i} 保留原声(vol={vol:.2f}) {slot:.1f}s",
                )
                _run(
                    [_ffmpeg(), "-y", "-i", str(base_a.resolve()), "-i", str(orig_a.resolve()),
                     "-filter_complex",
                     "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
                     "-map", "[a]", "-t", f"{slot:.3f}",
                     "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(stage_a.resolve())],
                    f"stage#{i} 配音+原声混音 {slot:.1f}s",
                )
            else:
                stage_a = base_a
            stage_audios.append(stage_a)

        video_all = work / "video_all.mp4"
        _concat_demux(stage_videos, video_all)
        audio_all = work / "audio_all.wav"
        concat_audio(stage_audios, audio_all)

        total_dur = sum(max(0.5, float(st.get("duration") or 0)) for st in stages)
        cmd = [_ffmpeg(), "-y", "-i", str(video_all.resolve()), "-i", str(audio_all.resolve())]
        use_bgm = bgm is not None and Path(bgm).exists()
        if use_bgm:
            # 无限循环 BGM 输入，靠 atrim 截到成片总长（垫在配音下，配音优先）
            cmd += ["-stream_loop", "-1", "-i", str(Path(bgm).resolve())]

        filters: list[str] = []
        vmap = "0:v:0"
        amap = "1:a:0"
        if srt is not None:
            shutil.copyfile(srt, work / "subs.srt")
            style = "force_style='Alignment=2,MarginV=60,Fontsize=16,Outline=1,Shadow=0'"
            filters.append(f"[0:v]subtitles=subs.srt:{style}[v]")
            vmap = "[v]"
        if use_bgm:
            vol = max(0.0, min(1.0, float(bgm_volume)))
            # BGM 压低 + 截长；与配音 amix（normalize=0 保配音满音量，duration=first 取配音长度）
            filters.append(
                f"[2:a]volume={vol:.3f},atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS[bg]"
            )
            filters.append(
                "[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
            )
            amap = "[a]"
        if filters:
            cmd += ["-filter_complex", ";".join(filters)]
        cmd += [
            "-map", vmap, "-map", amap, "-shortest",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
            "-video_track_timescale", "30000", str(output.resolve()),
        ]
        _run(cmd, f"结构合成 {len(stages)} 段 -> {output.name}{' +BGM' if use_bgm else ''}", cwd=work)
        return output
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _media_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def make_silence(duration_ms: int, dst: Path, sample_rate: int = 44100) -> Path:
    """生成一段指定时长的静音 wav（用于句间停顿）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    seconds = max(0.0, duration_ms / 1000.0)
    cmd = [
        _ffmpeg(), "-y", "-f", "lavfi",
        "-i", f"anullsrc=channel_layout=stereo:sample_rate={sample_rate}",
        "-t", f"{seconds:.3f}", "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "2",
        str(dst),
    ]
    _run(cmd, f"静音 {duration_ms}ms")
    return dst


def apply_speed_pitch(
    src: Path,
    dst: Path,
    tempo: float = 1.0,
    pitch_pct: float = 0.0,
    sample_rate: int = 44100,
) -> Path:
    """统一处理变速(tempo)与微调音高(pitch_pct，保持时长)。

    - 音高：asetrate 变调 + atempo 复原时长（±2% 只轻改音色、几乎不改语速）。
    - 语速：再叠一个 atempo=tempo（给 edge/eleven/cartesia 等不原生变速的 Provider）。
    tempo≈1 且 pitch≈0 时直接拷贝。
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tempo = max(0.5, min(2.0, tempo))
    if abs(pitch_pct) < 0.01 and abs(tempo - 1.0) < 0.001:
        shutil.copyfile(src, dst)
        return dst
    pratio = max(0.5, min(2.0, 1.0 + pitch_pct / 100.0))
    filters = [f"asetrate={sample_rate}*{pratio:.5f}", f"aresample={sample_rate}", f"atempo={1/pratio:.5f}"]
    if abs(tempo - 1.0) >= 0.001:
        filters.append(f"atempo={tempo:.5f}")
    af = ",".join(filters)
    cmd = [
        _ffmpeg(), "-y", "-i", str(src), "-af", af,
        "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", "2", str(dst),
    ]
    _run(cmd, f"变速{tempo:.3f}/变调{pitch_pct:+.1f}%")
    return dst


def concat_audio(clips: list[Path], output: Path) -> Path:
    """把多段音频（同格式 wav）顺序拼接为一段（配音用）。"""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    list_file = output.parent / f"{output.stem}_alist.txt"
    list_file.write_text(
        "\n".join(f"file '{Path(p).resolve().as_posix()}'" for p in clips),
        encoding="utf-8",
    )
    cmd = [
        _ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2", str(output),
    ]
    try:
        _run(cmd, f"拼接配音 {len(clips)} 段 -> {output.name}")
    finally:
        list_file.unlink(missing_ok=True)
    return output


def _run(cmd: list[str], desc: str, cwd: Path | None = None) -> None:
    logger.info("ffmpeg {} ...", desc)
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, cwd=str(cwd) if cwd else None
    )
    if result.returncode != 0:
        tail = (result.stderr or "")[-800:]
        raise RuntimeError(f"ffmpeg 失败({desc}): {tail}")
