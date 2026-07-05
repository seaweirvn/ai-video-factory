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
