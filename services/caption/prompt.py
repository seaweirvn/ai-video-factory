"""Prompt Library 加载器：按 language -> en -> default 回落读取每阶段 prompt。

prompt_library/{language}/{stage}.yaml，字段：
  stage / goal / tone / rules / good_examples / bad_examples / output_format
不在代码里写死任何字幕文案；所有规则/示例都在 yaml 里，改文案改 yaml 即可。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

# 项目根：services/caption/prompt.py -> parents[2] = ai-video-factory/
PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompt_library"

STAGES = ("hook", "value", "proof", "cta")
FALLBACK_LANG = "en"       # 找不到目标语言时优先回落英文
DEFAULT_LANG = "default"   # 英文也没有时回落语言中立通用 prompt

# 四个固定的 SubtitleIntent.type <-> video_stage 映射（供 resolver 接入用）
INTENT_TO_STAGE = {
    "hook_strong_attraction": "hook",
    "value_benefit": "value",
    "proof_claim": "proof",
    "cta_purchase": "cta",
}

# language(market) -> 人读国家名，喂给 prompt 让文案本地化到具体国家语感
LANG_COUNTRY = {
    "vi": "Vietnam",
    "th": "Thailand",
    "id": "Indonesia",
    "ms": "Malaysia",
    "en": "Global English market",
    "zh": "China",
}

# language -> 语言全名（output_format 里 {language_name} 用；default 目录自带占位）
LANG_NAME = {
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
    "en": "English",
    "zh": "Chinese",
}


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了不应炸渲染
        logger.warning("读取 prompt yaml 失败({})：{}", path, exc)
        return {}


@lru_cache(maxsize=128)
def load_prompt(language: str, stage: str) -> tuple[dict, str]:
    """加载 (prompt字典, 实际生效语言)。

    回落顺序：language -> en -> default。全都没有则返回 ({}, "")。
    """
    stage = (stage or "").strip().lower()
    lang = (language or "").strip().lower()
    for cand in [lang, FALLBACK_LANG, DEFAULT_LANG]:
        if not cand:
            continue
        data = _load_yaml(PROMPT_DIR / cand / f"{stage}.yaml")
        if data:
            return data, cand
    logger.warning("prompt_library 未找到任何可用 prompt - language={} stage={}", language, stage)
    return {}, ""


@lru_cache(maxsize=16)
def load_style(language: str) -> dict:
    """加载某语言的「口语风格 + 领域术语表 + 禁用词」（prompt_library/{lang}/_style.yaml）。

    可选文件：缺失则返回 {}（不影响生成）。用于把地道口语/正确行业术语/禁忌硬约束
    统一喂给 LLM，避免弱模型翻译腔、瞎编术语、夹带英文。字段：
      spoken_style(str) / glossary(list[str]) / forbidden(list[str]) / must(list[str])
    """
    lang = (language or "").strip().lower()
    if not lang:
        return {}
    data = _load_yaml(PROMPT_DIR / lang / "_style.yaml")
    return data if isinstance(data, dict) else {}


def language_name(language: str) -> str:
    return LANG_NAME.get((language or "").strip().lower(), language or "the target language")


def country_name(language: str, override: str = "") -> str:
    if override:
        return override
    return LANG_COUNTRY.get((language or "").strip().lower(), "the target market")


def stage_for_intent(intent_type: str) -> str:
    """SubtitleIntent.type -> video_stage；未知则原样返回（已是 stage 名时直接可用）。"""
    key = (intent_type or "").strip().lower()
    return INTENT_TO_STAGE.get(key, key)
