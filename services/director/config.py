"""Director 的 config 加载：config/playbooks.yaml（带缓存）。

结构/beat 全部在 yaml 里，代码不写死。改结构改 yaml 即可。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了不应炸整条生产
        logger.warning("读取 yaml 失败({})：{}", path, exc)
        return {}


@lru_cache(maxsize=1)
def load_playbooks() -> dict:
    return _load_yaml(CONFIG_DIR / "playbooks.yaml")


@lru_cache(maxsize=1)
def load_video_constraints() -> dict:
    """成片硬约束（角色数量 min/max、clip_max_duration、video_duration min/max）。"""
    data = _load_yaml(CONFIG_DIR / "video_constraints.yaml")
    # 安全默认，缺配置也不炸
    data.setdefault("roles", {
        "HOOK": {"min": 0, "max": 1},
        "VALUE": {"min": 1, "max": 3},
        "PROOF": {"min": 0, "max": 2},
        "CTA": {"min": 1, "max": 1},
    })
    data.setdefault("clip_max_duration", 10)
    data.setdefault("video_duration", {"min": 15, "max": 30})
    return data


def default_playbook() -> str:
    return str(load_playbooks().get("default_playbook") or "hook_value_proof_cta")


def playbook_names() -> list[str]:
    return list((load_playbooks().get("playbooks") or {}).keys())


def get_playbook(name: str) -> dict:
    return (load_playbooks().get("playbooks") or {}).get(name) or {}


def get_beat_def(name: str) -> dict:
    return (load_playbooks().get("beats") or {}).get(name) or {}


def pick_variant_total(total_available_sec: float) -> tuple[str, int]:
    """按可用素材总时长选 variant 与目标总时长。返回 (variant, total_sec)。"""
    pb = load_playbooks()
    threshold = float(pb.get("min_full_material_sec") or 18)
    variants = pb.get("variants") or {}
    variant = "full" if total_available_sec >= threshold else "compact"
    total = int((variants.get(variant) or {}).get("total") or (25 if variant == "full" else 15))
    return variant, total
