"""Speech Knowledge Base 加载 + token-aware 读音替换。

不是简单 str.replace：
- 整词匹配（前后必须是非字母数字，"S2" 不会命中 "S20" / "XS2"）；
- 大小写不敏感；多词短语与长词优先（正则按长度降序 alternation）；
- 知识库缺失 / 任意异常 -> 返回原文，绝不报错、绝不中断生产。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

from app.config import get_settings

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
SPEECH_DIR = CONFIG_DIR / "speech"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - 配置坏了不该炸渲染
        logger.warning("读取 speech KB 失败({})：{}", path, exc)
        return {}


@lru_cache(maxsize=16)
def load_speech_kb(language: str) -> dict:
    """加载某语言的 Speech Knowledge Base。

    返回 {"map": {lower_term: pronunciation}, "pattern": re.Pattern|None}。
    文件缺失 / terms 为空 -> map 空、pattern None（Formatter 变成 no-op）。
    """
    lang = (language or "").strip().lower()
    data = _load_yaml(SPEECH_DIR / f"{lang}.yaml") if lang else {}
    terms = data.get("terms") if isinstance(data, dict) else None
    boundary = (
        str(data.get("word_boundary") or "unicode").strip().lower()
        if isinstance(data, dict) else "unicode"
    )

    mapping: dict[str, str] = {}
    if isinstance(terms, dict):
        for k, v in terms.items():
            key = str(k).strip()
            val = str(v).strip()
            if key and val:
                mapping[key.lower()] = val

    pattern = None
    if mapping:
        # 长词优先：短语/更长 term 先匹配，避免被子串抢先
        keys = sorted(mapping.keys(), key=len, reverse=True)
        alt = "|".join(re.escape(k) for k in keys)
        if boundary == "ascii":
            # 拉丁词嵌在 CJK 文本里（如 "用SEAWEIR"、"这SEAWEIR"）：只把 ASCII 字母/数字
            # 当作边界，紧邻的中文字符不会挡住匹配；仍避免 "S2" 命中 "S20"/"XS2"。
            pattern = re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])", re.IGNORECASE)
        else:
            # 边界（Unicode 感知，兼容越南语带声调字母）：
            #   [^\W\d_] = 任意 Unicode 字母（含 á/ạ/ơ...）；[^\W_] = 字母或数字。
            # - 左边不能是字母（允许数字，故 "30mm"/"5kg"/"8+1" 这类「数字+单位」也能命中；
            #   而 "Máy"/"mạnh"/"item" 里的 "m" 因左/右是越南语字母而不会误伤）；
            # - 右边不能是字母或数字（"S2" 不会命中 "S20"）。
            pattern = re.compile(rf"(?<![^\W\d_])(?:{alt})(?![^\W_])", re.IGNORECASE)
    return {"map": mapping, "pattern": pattern}


def format_for_tts(text: str, language: str) -> str:
    """把 tts_text 里的已知品牌/型号/缩写/术语换成知识库标准读音（token-aware）。

    - 未命中的词保留原样（即 LLM 生成的 tts_text）。
    - 任意异常都返回原文，绝不打断配音。
    """
    if not text:
        return text
    try:
        if not getattr(get_settings(), "speech_formatter_enabled", True):
            return text
        kb = load_speech_kb(language)
        pattern = kb.get("pattern")
        mapping = kb.get("map")
        if not pattern or not mapping:
            return text

        def _repl(m: re.Match) -> str:
            val = mapping.get(m.group(0).lower(), m.group(0))
            # 「数字+单位」读音与数字之间补个空格，避免 "250Gam" 连读（-> "250 Gam"）
            i = m.start()
            if i > 0 and m.string[i - 1].isdigit():
                return " " + val
            return val

        return pattern.sub(_repl, text)
    except Exception as exc:  # noqa: BLE001 - 读音替换失败保留原文
        logger.warning("Speech Formatter 处理失败(保留原文) - {}", exc)
        return text
