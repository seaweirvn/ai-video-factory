"""在线 BGM Provider：按「国家 + 情绪」从在线曲库检索免版权曲、下载并缓存。

设计要点（安全增量）：
- 可插拔：_BaseProvider 抽象，实现 MagnificProvider（默认）/ JamendoProvider（备用）；换源只加实现。
- 全流程（检索+下载）受超时保护；任何异常/超时/无 key 返回 None，
  由上层 select_bgm 静默回落本地音乐库 -> 无 BGM，绝不打断成片。
- 下载即缓存（cache_dir/<provider>/<id>.<ext>），命中缓存不重复下载。
- 每国维护「最近用过」清单，避免连续重复选到同一首。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

import httpx
from loguru import logger

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class OnlineTrack:
    """在线命中的曲目：本地缓存路径 + 署名/元信息（供合规署名与音乐库登记）。"""

    path: Path
    provider: str
    track_id: str
    title: str = ""
    artist: str = ""
    license_name: str = ""
    license_url: str = ""
    source_url: str = ""

    @property
    def key(self) -> str:
        """音乐库/评分统一键：provider:track_id。"""
        return f"{self.provider}:{self.track_id}"

    def attribution(self) -> str:
        """一行署名文本。"""
        who = f"{self.title} by {self.artist}".strip(" by ")
        parts = [p for p in [who, self.license_name, self.source_url] if p]
        return " — ".join(parts)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "provider": self.provider,
            "track_id": self.track_id,
            "title": self.title,
            "artist": self.artist,
            "license_name": self.license_name,
            "license_url": self.license_url,
            "source_url": self.source_url,
            "attribution": self.attribution(),
        }


class _BaseProvider:
    """通用能力：标签构建、每国最近避免、流式下载、缓存目录。"""

    name = "base"

    def __init__(self, cfg: dict, settings) -> None:
        self.cfg = cfg or {}
        self.s = settings
        self.timeout = float(getattr(settings, "bgm_online_timeout_sec", 15.0) or 15.0)
        self.recent_avoid = int(self.cfg.get("recent_avoid") or 0)
        cache_dir = str(self.cfg.get("cache_dir") or "assets/bgm/cache")
        self.cache_dir = ROOT / cache_dir / self.name

    # ---- 国家风格 + 情绪 标签（子类决定读 genre 还是 fuzzytags）----
    def _country_field(self, country: str, field: str) -> list[str]:
        cq = (self.cfg.get("country_query") or {}).get((country or "").strip().upper()) or {}
        return list(cq.get(field) or [])

    def _mood_tags(self, audio_mood: str) -> list[str]:
        return list((self.cfg.get("mood_tags") or {}).get((audio_mood or "").strip().lower()) or [])

    @staticmethod
    def _dedup(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for t in items:
            t = str(t).strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out

    # ---- 每国「最近用过」避免重复 ----
    def _recent_path(self, country: str) -> Path:
        return self.cache_dir / f".recent_{(country or 'xx').lower()}.json"

    def _load_recent(self, country: str) -> list[str]:
        p = self._recent_path(country)
        if not p.exists():
            return []
        try:
            return list(json.loads(p.read_text(encoding="utf-8")) or [])
        except Exception:  # noqa: BLE001
            return []

    def _save_recent(self, country: str, track_id: str) -> None:
        if self.recent_avoid <= 0:
            return
        try:
            ids = [i for i in self._load_recent(country) if i != track_id]
            ids.append(track_id)
            ids = ids[-self.recent_avoid:]
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._recent_path(country).write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug("BGM recent 写入失败(忽略) - {}", exc)

    def _stream_download(self, url: str, dest: Path, headers: dict | None = None) -> bool:
        if dest.exists() and dest.stat().st_size > 0:
            return True  # 命中缓存
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as cli:
                with cli.stream("GET", url, headers=headers or {}) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(65536):
                            f.write(chunk)
            tmp.replace(dest)
        except Exception as exc:  # noqa: BLE001 - 下载失败静默回落
            logger.warning("在线 BGM 下载失败(回落) - provider={} url={} err={}", self.name, url[:80], exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return False
        return dest.exists() and dest.stat().st_size > 0

    def fetch(self, audio_mood: str, country: str, *, rng: random.Random | None = None
              ) -> OnlineTrack | None:
        raise NotImplementedError


class MagnificProvider(_BaseProvider):
    """Magnific Music API（默认）：genre+mood 检索、免版权、下载 file/download_url 缓存。"""

    name = "magnific"

    # Magnific 合法枚举（Title Case）；配置里用小写自然词，这里规范化匹配。
    _GENRES = {
        "acoustic", "afrobeat", "ambient", "blues", "children", "cinematic", "classical",
        "corporate", "country", "disco", "electronic", "funk", "hip hop", "jazz", "latin",
        "lofi", "lounge", "pop", "reggae", "rnb", "rock", "soul", "synthwave", "world",
    }
    _MOODS = {
        "dark", "dramatic", "elegant", "energetic", "epic", "exciting", "groovy", "happy",
        "hopeful", "laid back", "melancholic", "peaceful", "playful", "sad", "sentimental",
        "soulful", "tension", "upbeat",
    }

    def __init__(self, cfg: dict, settings) -> None:
        super().__init__(cfg, settings)
        self.api_key = (getattr(settings, "magnific_api_key", "") or "").strip()
        self.base = (getattr(settings, "magnific_base_url", "") or "https://api.magnific.com").rstrip("/")

    @staticmethod
    def _to_enum(values: list[str], allowed: set[str]) -> list[str]:
        """小写自然词 -> Magnific Title-Case 枚举（只保留合法项）。"""
        out: list[str] = []
        for v in values:
            low = str(v).strip().lower()
            if low in allowed:
                out.append("RnB" if low == "rnb" else low.title())
        return out

    def fetch(self, audio_mood: str, country: str, *, rng: random.Random | None = None
              ) -> OnlineTrack | None:
        if not self.api_key:
            logger.info("在线选曲跳过：未配置 MAGNIFIC_API_KEY")
            return None
        genre = self._to_enum(self._dedup(self._country_field(country, "genre")), self._GENRES)
        mood = self._to_enum(self._dedup(self._mood_tags(audio_mood)), self._MOODS)
        params: dict = {
            "include-premium": "false",   # 只取免费/免版权
            "order_by": "-popularity",
            "limit": "40",
        }
        if genre:
            params["genre"] = ",".join(genre)
        if mood:
            params["mood"] = ",".join(mood)
        headers = {"x-magnific-api-key": self.api_key}
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as cli:
                resp = cli.get(f"{self.base}/v1/music", params=params, headers=headers)
                resp.raise_for_status()
                results = (resp.json() or {}).get("results") or []
        except Exception as exc:  # noqa: BLE001 - 检索失败静默回落
            logger.warning("在线选曲检索失败(回落) - provider=magnific err={}", exc)
            return None

        candidates = [t for t in results if t.get("id") and not t.get("is_premium")]
        if not candidates:
            logger.info("在线选曲无免费命中(回落) - mood={} country={} genre={} moods={}",
                        audio_mood, country, genre, mood)
            return None

        recent = set(self._load_recent(country))
        pool = [t for t in candidates if str(t.get("id")) not in recent] or candidates
        chosen = (rng or random).choice(pool)
        track_id = str(chosen.get("id"))

        url = (chosen.get("download_url") or chosen.get("file_url") or "").strip()
        if not url:  # 兜底：走单曲下载端点
            url = self._resolve_download_url(track_id, headers)
        if not url:
            return None

        dest = self.cache_dir / f"{track_id}.mp3"
        if not self._stream_download(url, dest, headers=headers):
            return None

        artist = ((chosen.get("artist") or {}) if isinstance(chosen.get("artist"), dict) else {})
        track = OnlineTrack(
            path=dest, provider="magnific", track_id=track_id,
            title=str(chosen.get("title") or ""),
            artist=str(artist.get("name") or ""),
            license_name="Magnific Royalty-Free",
            license_url="https://docs.magnific.com/legal/license",
            source_url=str(chosen.get("file_url") or ""),
        )
        self._save_recent(country, track_id)
        logger.info("在线选中 BGM - provider=magnific mood={} country={} id={} '{}'",
                    audio_mood, country, track_id, track.title)
        return track

    def _resolve_download_url(self, track_id: str, headers: dict) -> str:
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as cli:
                r = cli.get(f"{self.base}/v1/music/{track_id}/download", headers=headers)
                r.raise_for_status()
                return str((r.json() or {}).get("download_url") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Magnific 取下载链接失败 - id={} err={}", track_id, exc)
            return ""


# ==== Jamendo（备用 provider）：CC-BY 过滤 ====
def _license_allowed(license_url: str, policy: str) -> tuple[bool, str]:
    """返回 (是否允许, 可读授权名)。cc_by 允许 by / by-sa，排除 nc / nd。"""
    url = (license_url or "").lower()
    if "creativecommons.org" not in url:
        if "publicdomain" in url or "cc0" in url:
            return True, "CC0"
        return False, ""
    seg = ""
    for part in url.split("/"):
        if part.startswith("by"):
            seg = part
            break
    if not seg:
        return False, ""
    is_nc = "nc" in seg.split("-")
    is_nd = "nd" in seg.split("-")
    policy = (policy or "cc_by").lower()
    if policy == "cc_by_nc_ok":
        ok = not is_nd
    else:  # cc_by（默认）
        ok = not is_nc and not is_nd
    return ok, "CC " + seg.upper()


class JamendoProvider(_BaseProvider):
    """Jamendo v3 API（备用）：fuzzytags 检索 + popularity 排序 + CC-BY 过滤 + 下载缓存。"""

    name = "jamendo"
    API = "https://api.jamendo.com/v3.0"

    def __init__(self, cfg: dict, settings) -> None:
        super().__init__(cfg, settings)
        self.client_id = (getattr(settings, "jamendo_client_id", "") or "").strip()
        self.license_policy = str(self.cfg.get("license") or "cc_by")
        self.audioformat = str(self.cfg.get("audioformat") or "mp32")

    def _tags(self, audio_mood: str, country: str) -> list[str]:
        # jamendo 用 fuzzytags：国家 genre + 情绪 mood，统一小写
        tags = self._country_field(country, "genre") + self._country_field(country, "fuzzytags")
        tags += self._mood_tags(audio_mood)
        return [t.lower() for t in self._dedup(tags)]

    def fetch(self, audio_mood: str, country: str, *, rng: random.Random | None = None
              ) -> OnlineTrack | None:
        if not self.client_id:
            logger.info("在线选曲跳过：未配置 JAMENDO_CLIENT_ID")
            return None
        tags = self._tags(audio_mood, country)
        params = {
            "client_id": self.client_id, "format": "json", "limit": "40",
            "order": "popularity_total", "audiodownload_allowed": "true",
            "include": "musicinfo licenses", "audioformat": self.audioformat,
        }
        if tags:
            params["fuzzytags"] = " ".join(tags)
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as cli:
                resp = cli.get(f"{self.API}/tracks", params=params)
                resp.raise_for_status()
                results = (resp.json() or {}).get("results") or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("在线选曲检索失败(回落) - provider=jamendo err={}", exc)
            return None

        candidates: list[dict] = []
        for t in results:
            if not t.get("audiodownload_allowed") or not (t.get("audiodownload") or "").strip():
                continue
            ok, lic_name = _license_allowed(t.get("license_ccurl") or "", self.license_policy)
            if not ok:
                continue
            t["_license_name"] = lic_name
            candidates.append(t)
        if not candidates:
            logger.info("在线选曲无可商用命中(回落) - mood={} country={} tags={}", audio_mood, country, tags)
            return None

        recent = set(self._load_recent(country))
        pool = [t for t in candidates if str(t.get("id")) not in recent] or candidates
        chosen = (rng or random).choice(pool)
        track_id = str(chosen.get("id"))
        dest = self.cache_dir / f"{track_id}.mp3"
        if not self._stream_download((chosen.get("audiodownload") or "").strip(), dest):
            return None
        track = OnlineTrack(
            path=dest, provider="jamendo", track_id=track_id,
            title=str(chosen.get("name") or ""), artist=str(chosen.get("artist_name") or ""),
            license_name=str(chosen.get("_license_name") or "CC"),
            license_url=str(chosen.get("license_ccurl") or ""),
            source_url=str(chosen.get("shorturl") or chosen.get("shareurl") or ""),
        )
        self._save_recent(country, track_id)
        logger.info("在线选中 BGM - provider=jamendo mood={} country={} id={} '{}' [{}]",
                    audio_mood, country, track_id, track.title, track.license_name)
        return track


_PROVIDERS = {"magnific": MagnificProvider, "jamendo": JamendoProvider}


def fetch_online_bgm(
    audio_mood: str, country: str, cfg: dict, settings, *, rng: random.Random | None = None
) -> OnlineTrack | None:
    """按配置的 provider 在线选曲；不可用/失败返回 None（上层回落本地）。"""
    if not cfg or not cfg.get("enabled"):
        return None
    name = str(cfg.get("provider") or getattr(settings, "bgm_online_provider", "magnific")).lower()
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        logger.warning("未知在线 BGM provider={}（回落本地）", name)
        return None
    try:
        return provider_cls(cfg, settings).fetch(audio_mood, country, rng=rng)
    except Exception as exc:  # noqa: BLE001 - 任何异常都不阻塞成片
        logger.warning("在线选曲异常(回落本地) - provider={} err={}", name, exc)
        return None
