"""DirectorEngine 的 LLM 大脑：像专业导演一样先出 Creative Brief，再出自由 Story Beats。

- 输入：产品能力盘点(MaterialInventory) + 产品背景 + 市场/目标 + 成片硬约束(video_constraints)。
- 输出：Creative Brief（角度/卖点/情绪/风格）+ 一串 Story Beats（每拍：目的/想表达/所需镜头/
  建议时长/情绪/角色）。**不写最终字幕**，结构不写死（LLM 自由决定 question/problem/demo... 顺序）。
- 任何失败/无 key 返回 None，由 engine 回落启发式。

只返回原始解析结果；校验/归一在 engine 统一做（LLM 与启发式共用一套规范化）。
"""

from __future__ import annotations

import json

import httpx
from loguru import logger

from core.models import ProductProfile
from services.director.models import MaterialInventory


def generate_story(
    inventory: MaterialInventory,
    profile: ProductProfile | None,
    market: str,
    goal: str,
    constraints: dict,
    settings,
) -> dict | None:
    if not getattr(settings, "openai_api_key", ""):
        return None
    try:
        return _call(inventory, profile, market, goal, constraints, settings)
    except Exception as exc:  # noqa: BLE001 - LLM 失败绝不阻塞，回落启发式
        logger.warning("DirectorEngine LLM 生成失败，回落启发式 - {}", exc)
        return None


def _call(inventory, profile, market, goal, constraints, settings) -> dict | None:
    roles_cfg = constraints.get("roles") or {}
    role_max = {r: int((v or {}).get("max", 0)) for r, v in roles_cfg.items()}
    role_min = {r: int((v or {}).get("min", 0)) for r, v in roles_cfg.items()}
    clip_max = float(constraints.get("clip_max_duration") or 10)
    vdur = constraints.get("video_duration") or {}
    vmin, vmax = float(vdur.get("min") or 15), float(vdur.get("max") or 30)

    available_shots = sorted(inventory.shot_enum_counts.keys())
    available_roles = {r: c for r, c in inventory.role_counts.items()}
    ranked_sp = inventory.ranked_selling_points or []
    pos = (profile.positioning if profile else "") or ""
    aud = (profile.target_audience if profile else "") or ""
    forbidden = (profile.forbidden_words if profile else []) or []

    system = (
        "You are a professional short-video DIRECTOR for TikTok Shop conversion videos. "
        "Design ONE video like a real director: first a short Creative Brief (how to sell), "
        "then an ordered list of Story Beats. Beats are NOT a fixed template — freely choose the "
        "narrative that best drives conversion (e.g. question->demo->proof->cta, or "
        "problem->solution->demo->cta). Each beat states its PURPOSE, what to EXPRESS, the SHOT "
        "TYPES it needs, a suggested DURATION range, and the EMOTION to build. "
        "You do NOT write final captions. Output strict JSON only, and stay within the given "
        "material inventory and hard constraints."
    )
    user = (
        f"product: {inventory.product}\nmarket: {market}\ngoal: {goal}\n"
        f"positioning: {pos}\naudience: {aud}\nforbidden_words: {forbidden}\n\n"
        f"AVAILABLE selling points (evidence-ranked, pick core from here): {ranked_sp}\n"
        f"AVAILABLE shot types (use ONLY these enum keys in beats.shots): {available_shots}\n"
        f"AVAILABLE material roles (role: count): {available_roles}\n\n"
        f"HARD CONSTRAINTS (must respect):\n"
        f"- role clip-count max per video: {role_max}; role min: {role_min}\n"
        f"- each beat is one continuous shot; beat max_sec <= {clip_max}s (no fast cuts)\n"
        f"- total video duration must be between {vmin} and {vmax} seconds\n"
        f"- number of beats using each role must not exceed that role's max\n\n"
        "Return ONLY JSON with this shape:\n"
        "{\n"
        '  "angle": "one sentence: what this video is about",\n'
        '  "core_selling_point": "one of AVAILABLE selling points",\n'
        '  "supporting_points": ["subset of selling points"],\n'
        '  "emotion": "overall emotion (desire|trust|curiosity|urgency)",\n'
        '  "tone": "short phrase",\n'
        '  "caption_style": "short phrase",\n'
        '  "audio_mood": "energetic|upbeat|chill|suspense",\n'
        '  "rationale": "why this structure sells",\n'
        '  "beats": [\n'
        '    {"name": "free label e.g. question/problem/demo/proof/cta",\n'
        '     "purpose": "...", "express": "...", "emotion": "...",\n'
        '     "role": "HOOK|VALUE|PROOF|CTA", "shots": ["enum", "enum"],\n'
        '     "min_sec": 1, "max_sec": 3}\n'
        "  ]\n"
        "}\n"
        "Order the beats to follow a real conversion arc and end with a CTA beat."
    )

    resp = httpx.post(
        f"{settings.openai_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        json={
            "model": settings.openai_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.8,
            "response_format": {"type": "json_object"},
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    data = json.loads(resp.json()["choices"][0]["message"]["content"])
    if not isinstance(data, dict) or not data.get("beats"):
        return None
    logger.info(
        "DirectorEngine LLM 出稿 - product={} beats={} core_sp={}",
        inventory.product, len(data.get("beats") or []), data.get("core_selling_point"),
    )
    return data
