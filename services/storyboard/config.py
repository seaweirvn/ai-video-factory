"""Storyboard/字幕 相关的 config 加载（config/ 下的 yaml，带缓存）。

所有规则（结构时长、镜头优先级、卖点枚举、标签映射、字幕模板）都在 config 里，
代码不写死。改规则改 yaml 即可，无需改代码。
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

# 项目根：services/storyboard/config.py -> parents[2] = ai-video-factory/
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
LOCALES_DIR = CONFIG_DIR / "locales"
DEFAULT_MARKET = "vi"  # 缺失 market 配置时回落到的默认市场

# 写回 locale yaml 时用（AI 自增长模板库），避免并发写坏文件
_WRITE_LOCK = threading.Lock()


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了不应炸整条渲染
        logger.warning("读取 yaml 失败({})：{}", path, exc)
        return {}


@lru_cache(maxsize=1)
def load_structure() -> dict:
    return _load_yaml(CONFIG_DIR / "structure.yaml")


@lru_cache(maxsize=1)
def load_shot_priority() -> dict:
    return _load_yaml(CONFIG_DIR / "shot_priority.yaml")


@lru_cache(maxsize=1)
def load_selling_points() -> dict:
    return _load_yaml(CONFIG_DIR / "selling_points.yaml")


@lru_cache(maxsize=1)
def load_tag_mapping() -> dict:
    return _load_yaml(CONFIG_DIR / "tag_mapping.yaml")


def locale_path(market: str) -> Path:
    return LOCALES_DIR / market / "subtitles.yaml"


def locale_exists(market: str) -> bool:
    return locale_path(market).exists()


@lru_cache(maxsize=16)
def load_locale_subtitles(market: str) -> dict:
    """加载某市场的字幕模板池；文件不存在返回 {}（调用方据此判断是否回落）。"""
    return _load_yaml(locale_path(market))


def default_selling_point() -> str:
    return str(load_selling_points().get("default") or "smooth_retrieve")


def is_known_selling_point(key: str) -> bool:
    return key in (load_selling_points().get("points") or {})


def resolve_selling_point(*raw_tags: str) -> str:
    """把飞书标签(中/越文本) 归一成英文卖点枚举。

    顺序：精确匹配(大小写不敏感/去空白) -> 子串包含 -> tag_mapping.default -> selling_points.default。
    """
    tm = load_tag_mapping()
    mapping = {str(k).strip().lower(): v for k, v in (tm.get("mapping") or {}).items()}
    candidates = [t.strip() for t in raw_tags if t and t.strip()]
    # 1) 精确
    for t in candidates:
        hit = mapping.get(t.lower())
        if hit:
            return str(hit)
    # 2) 子串包含（标签里包含某个映射键）
    for t in candidates:
        tl = t.lower()
        for key, val in mapping.items():
            if key and key in tl:
                return str(val)
    # 3) 兜底
    return str(tm.get("default") or default_selling_point())


def append_locale_default(market: str, intent_type: str, text: str) -> bool:
    """把 AI 生成的文案异步回写到 {market}/subtitles.yaml 的 intent_type.default 分组（去重）。"""
    path = locale_path(market)
    if not path.exists():
        return False
    with _WRITE_LOCK:
        data = _load_yaml(path)
        node = data.setdefault(intent_type, {})
        if not isinstance(node, dict):
            node = {}
            data[intent_type] = node
        bucket = node.setdefault("default", [])
        if not isinstance(bucket, list):
            bucket = []
            node["default"] = bucket
        if text in bucket:
            return False
        bucket.append(text)
        try:
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("回写字幕模板失败({})：{}", path, exc)
            return False
    load_locale_subtitles.cache_clear()  # 让下次读取到最新
    logger.info("字幕模板已自增长 - market={} intent={} +1", market, intent_type)
    return True
