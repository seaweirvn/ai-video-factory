"""本地音乐库：把在线下载过的曲目登记下来，之后按「国家 + 情绪」复用，不重复下载。

- 存储：data/bgm_library.json（{"tracks": [...]}），每条记录曲目元信息 + mood/country + 本地路径。
- 复用：candidates(country, mood) 逐级兜底列出可复用曲目（文件仍存在才算数）。
- 与 GMV 打分配合：选曲时按 PerformanceStore.bgm_score(key) 加权复用高分曲（key=provider:track_id）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from loguru import logger

from services.director.bgm_online import OnlineTrack

ROOT = Path(__file__).resolve().parents[2]
LIBRARY_PATH = ROOT / "data" / "bgm_library.json"


class BgmLibrary:
    def __init__(self, path: Path = LIBRARY_PATH) -> None:
        self.path = Path(path)
        self._tracks: list[dict] = []
        self._loaded = False

    def load(self) -> None:
        self._tracks = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8")) or {}
                self._tracks = list(data.get("tracks") or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 bgm_library.json 失败：{}", exc)
                self._tracks = []
        self._loaded = True

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"tracks": self._tracks}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入 bgm_library.json 失败：{}", exc)

    @staticmethod
    def _abspath(rel_or_abs: str) -> Path:
        p = Path(rel_or_abs)
        return p if p.is_absolute() else (ROOT / p)

    def register(self, track: OnlineTrack, mood: str, country: str) -> None:
        """把在线命中的曲目登记入库（按 key 去重；已存在则补充 mood/country 覆盖）。"""
        self._ensure()
        key = track.key
        try:
            rel = str(track.path.resolve().relative_to(ROOT))
        except Exception:  # noqa: BLE001 - 不在项目内则存绝对路径
            rel = str(track.path.resolve())
        rec = {
            "key": key,
            "provider": track.provider,
            "track_id": track.track_id,
            "path": rel,
            "title": track.title,
            "artist": track.artist,
            "license_name": track.license_name,
            "license_url": track.license_url,
            "source_url": track.source_url,
            "mood": (mood or "").strip().lower(),
            "country": (country or "").strip().upper(),
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        for i, t in enumerate(self._tracks):
            if t.get("key") == key:
                rec["added_at"] = t.get("added_at", rec["added_at"])
                self._tracks[i] = rec
                self._save()
                return
        self._tracks.append(rec)
        self._save()
        logger.info("音乐库登记 - {} '{}' mood={} country={}", key, track.title, rec["mood"], rec["country"])

    def _alive(self, rec: dict) -> bool:
        p = self._abspath(str(rec.get("path") or ""))
        return p.exists() and p.stat().st_size > 0

    def candidates(self, country: str, mood: str) -> list[dict]:
        """逐级兜底：国家+情绪 -> 同情绪(任意国家) -> 同国家(任意情绪) -> 全部。仅返回文件仍在的。"""
        self._ensure()
        alive = [t for t in self._tracks if self._alive(t)]
        if not alive:
            return []
        c = (country or "").strip().upper()
        m = (mood or "").strip().lower()
        exact = [t for t in alive if t.get("country") == c and t.get("mood") == m]
        if exact:
            return exact
        by_mood = [t for t in alive if t.get("mood") == m]
        if by_mood:
            return by_mood
        by_country = [t for t in alive if t.get("country") == c]
        if by_country:
            return by_country
        return alive

    def to_track(self, rec: dict) -> OnlineTrack:
        return OnlineTrack(
            path=self._abspath(str(rec.get("path") or "")),
            provider=str(rec.get("provider") or ""),
            track_id=str(rec.get("track_id") or ""),
            title=str(rec.get("title") or ""),
            artist=str(rec.get("artist") or ""),
            license_name=str(rec.get("license_name") or ""),
            license_url=str(rec.get("license_url") or ""),
            source_url=str(rec.get("source_url") or ""),
        )


@lru_cache(maxsize=1)
def get_bgm_library() -> BgmLibrary:
    return BgmLibrary()
