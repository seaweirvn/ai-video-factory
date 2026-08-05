"""BgmSelector：按「情绪 + 国家」选一条 BGM，优先复用音乐库里 GMV 高分曲。

选曲策略：
  1) 音乐库复用（exploit）：按 PerformanceStore.bgm_score（GMV 贝叶斯收缩）加权挑库内曲，
     经常复用成交好的曲子；ε 概率转为「探索」。
  2) 在线拉新（explore / 库为空）：从在线曲库（默认 Magnific）检索免版权曲、下载入库。
  3) 静态本地曲库（config/bgm.yaml 手放的文件）。
  4) None —— 成片不加 BGM（安全兜底）。

任何在线异常/超时/无 key 都静默回落，绝不打断成片。
是否加 BGM 的 50% 概率开关在 produce 层控制（每条视频独立掷骰）。
"""

from __future__ import annotations

import random
from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

from app.config import get_settings
from services.director.bgm_library import get_bgm_library
from services.director.bgm_online import OnlineTrack, fetch_online_bgm
from services.selection.performance import get_performance_store

# 项目根：services/director/bgm.py -> parents[2]
ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "bgm.yaml"


@lru_cache(maxsize=1)
def _load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 bgm.yaml 失败：{}", exc)
        return {}


def _select_local(
    audio_mood: str, country: str, cfg: dict, *, rng: random.Random | None = None
) -> Path | None:
    """静态本地曲库：先 countries.<国家>.<情绪>，再全局 moods.<情绪>。文件不存在则跳过。"""
    mood = (audio_mood or "").strip().lower()
    if not mood or not cfg:
        return None
    base_dir = ROOT / str(cfg.get("base_dir") or "assets/bgm")

    tracks: list[str] = []
    by_country = (cfg.get("countries") or {}).get((country or "").strip().upper()) or {}
    if isinstance(by_country, dict) and by_country.get(mood):
        tracks = list(by_country.get(mood) or [])
    if not tracks:
        tracks = list((cfg.get("moods") or {}).get(mood) or [])

    candidates = [base_dir / t for t in tracks]
    candidates = [p for p in candidates if p.exists()]
    if not candidates:
        return None
    chosen = (rng or random).choice(candidates)
    logger.info("BGM 静态本地选中 - mood={} country={} -> {}", mood, country, chosen.name)
    return chosen.resolve()


def _online_cfg(cfg: dict, settings) -> dict | None:
    """在线选曲配置（settings 或 yaml 任一开启即启用）；未启用返回 None。"""
    oc = dict(cfg.get("online") or {})
    if bool(getattr(settings, "bgm_online_enabled", False)) or bool(oc.get("enabled")):
        oc["enabled"] = True
        return oc
    return None


def _exploit_library(country: str, mood: str, rng: random.Random) -> tuple[Path, OnlineTrack] | None:
    """按 GMV 分加权从音乐库挑一条可复用曲。"""
    lib = get_bgm_library()
    cands = lib.candidates(country, mood)
    if not cands:
        return None
    perf = get_performance_store()
    weights = [max(perf.bgm_score(str(c.get("key") or "")), 0.01) for c in cands]
    chosen = rng.choices(cands, weights=weights, k=1)[0]
    track = lib.to_track(chosen)
    logger.info(
        "BGM 库内复用 - key={} '{}' score={:.3f} mood={} country={}",
        track.key, track.title, perf.bgm_score(track.key), mood, country,
    )
    return track.path, track


def select_bgm_detailed(
    audio_mood: str, country: str = "", *, rng: random.Random | None = None
) -> tuple[Path | None, OnlineTrack | None]:
    """返回 (BGM 文件路径, 曲目信息)。库内复用/在线拉新都带曲目信息（含 key，用于 GMV 归因）。"""
    cfg = _load()
    if not cfg:
        return None, None
    r = rng or random
    s = get_settings()
    mood = (audio_mood or "").strip().lower()

    online = _online_cfg(cfg, s)
    lib = get_bgm_library()
    has_lib = bool(lib.candidates(country, mood))

    # 探索：ε 概率或库空时，去在线拉新曲扩库
    eps = float(getattr(s, "director_bgm_explore_epsilon", 0.25) or 0.0)
    want_new = online is not None and (r.random() < eps or not has_lib)
    if want_new:
        track = fetch_online_bgm(audio_mood, country, online, s, rng=r)
        if track and track.path.exists():
            lib.register(track, mood, country)
            return track.path, track
        # 在线失败 -> 回落库内复用/静态本地

    # 利用：按 GMV 分复用库内曲
    reused = _exploit_library(country, mood, r)
    if reused is not None:
        return reused

    # 静态本地曲库
    local = _select_local(audio_mood, country, cfg, rng=r)
    if local is not None:
        return local, None

    return None, None


def select_bgm(
    audio_mood: str, country: str = "", *, rng: random.Random | None = None
) -> Path | None:
    """选一条 BGM 文件（绝对路径）；无命中/文件不存在返回 None。"""
    return select_bgm_detailed(audio_mood, country, rng=rng)[0]
