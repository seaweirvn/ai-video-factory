"""Brief 策略：决定「用哪个结构、主打哪个卖点、建立什么情绪」。

- HeuristicStrategy（默认）：确定性——按 inventory 可行性 + 证据 + 表现学习打分选结构/卖点，
  冷启动无数据时纯按可行性+证据，行为可预期、零额外成本。
- LlmStrategy（可选）：让 LLM 读产品能力盘点产出策略决策，经白名单校验；任何失败都回落 Heuristic。

两者都只做「销售方向」决策，绝不产出最终字幕。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from loguru import logger

from core.models import ProductProfile
from services.director import config as dcfg
from services.director.models import MaterialInventory

# 表现分对决策分的影响幅度（与 selection.scoring.PERF_INFLUENCE 同量纲）
PERF_INFLUENCE = 2.0


@dataclass
class StrategyDecision:
    """策略层的产出（结构 + 销售方向），由 DirectorEngine 装配成 Brief。"""

    playbook: str
    core_selling_point: str
    angle: str = ""
    emotion: str = ""
    tone: str = ""
    caption_style: str = ""
    audio_mood: str = ""
    supporting_points: list[str] = field(default_factory=list)
    scores: dict = field(default_factory=dict)
    rationale: str = ""
    needs_review: bool = False


class BriefStrategy(Protocol):
    def decide(
        self, inventory: MaterialInventory, profile: ProductProfile | None, market: str, goal: str
    ) -> StrategyDecision: ...


def _playbook_feasibility(playbook_name: str, inventory: MaterialInventory) -> float:
    """结构可行性：各 beat 的 roles 在库里被覆盖的比例（0~1）；镜头覆盖再给小加成。"""
    beats = dcfg.get_playbook(playbook_name).get("beats") or []
    if not beats:
        return 0.0
    covered = 0.0
    for beat_name in beats:
        bd = dcfg.get_beat_def(beat_name)
        roles = [str(r).upper() for r in (bd.get("roles") or [])]
        role_ok = any(inventory.role_counts.get(r, 0) > 0 for r in roles)
        if role_ok:
            covered += 1.0
            shots = list(bd.get("shots") or [])
            if shots and inventory.has_shots(shots):
                covered += 0.15  # 有对味镜头再加一点
    return covered / max(1, len(beats))


class HeuristicStrategy:
    def __init__(self, perf=None, epsilon: float = 0.2, perf_opt: float = 0.6) -> None:
        self.perf = perf
        self.epsilon = epsilon
        self.perf_opt = perf_opt
        self._rng_seed = None

    def decide(
        self, inventory: MaterialInventory, profile: ProductProfile | None, market: str, goal: str
    ) -> StrategyDecision:
        import random

        rng = random.Random(self._rng_seed)

        # 1) 核心卖点：inventory 已按「证据(+表现)」排序，取最硬者
        core_sp = inventory.ranked_selling_points[0] if inventory.ranked_selling_points else ""

        # 2) 结构：只在可行结构里选；分 = 可行性 + 表现学习
        names = dcfg.playbook_names() or [dcfg.default_playbook()]
        pb_perf = self.perf.playbook_score if (self.perf and self.perf.has_playbook_data) else None
        scored: list[tuple[float, str]] = []
        for name in names:
            feas = _playbook_feasibility(name, inventory)
            if feas <= 0.0:
                continue
            perf = PERF_INFLUENCE * (float(pb_perf(name)) - self.perf_opt) if pb_perf else 0.0
            scored.append((feas + perf, name))
        if not scored:
            scored = [(0.0, dcfg.default_playbook())]
        scored.sort(key=lambda t: (t[0], rng.random()), reverse=True)

        # 探索/利用：epsilon 概率在可行结构里随机探索（A/B 学不同结构的成交表现）
        if len(scored) > 1 and rng.random() < self.epsilon:
            playbook = rng.choice([n for _, n in scored])
        else:
            playbook = scored[0][1]

        pb_cfg = dcfg.get_playbook(playbook)
        emotion = str(pb_cfg.get("emotion") or "desire")
        audio_mood = str(pb_cfg.get("audio_mood") or "energetic")

        supporting = [sp for sp in inventory.ranked_selling_points[1:4] if sp and sp != core_sp]
        angle = self._compose_angle(inventory.product, core_sp, profile)
        caption_style = self._compose_style(emotion, profile)

        return StrategyDecision(
            playbook=playbook,
            core_selling_point=core_sp,
            angle=angle,
            emotion=emotion,
            tone="",  # 逐 beat 自带 tone
            caption_style=caption_style,
            audio_mood=audio_mood,
            supporting_points=supporting,
            scores={"playbooks": {n: round(s, 3) for s, n in scored}},
            rationale=(
                f"core_sp={core_sp} 证据最硬；playbook={playbook} "
                f"(可行性+表现分最高{'，含表现学习' if pb_perf else '，冷启动纯可行性'})"
            ),
        )

    @staticmethod
    def _compose_angle(product: str, core_sp: str, profile: ProductProfile | None) -> str:
        aud = f" for {profile.target_audience}" if (profile and profile.target_audience) else ""
        return f"Sell {product or 'this product'} on its '{core_sp or 'main benefit'}'{aud}."

    @staticmethod
    def _compose_style(emotion: str, profile: ProductProfile | None) -> str:
        base = f"native, spoken, {emotion}-driven short-video copy"
        if profile and profile.forbidden_words:
            base += "; avoid: " + ", ".join(profile.forbidden_words[:6])
        return base


class LlmStrategy:
    """可选 LLM 策略：读盘点产策略；任何失败/无 key 回落 Heuristic。结构/卖点经白名单校验。"""

    def __init__(self, settings, perf=None, epsilon: float = 0.2) -> None:
        self.s = settings
        self.fallback = HeuristicStrategy(perf=perf, epsilon=epsilon)

    def decide(
        self, inventory: MaterialInventory, profile: ProductProfile | None, market: str, goal: str
    ) -> StrategyDecision:
        base = self.fallback.decide(inventory, profile, market, goal)
        if not getattr(self.s, "openai_api_key", ""):
            return base
        try:
            decision = self._call_llm(inventory, profile, market, goal, base)
            return decision or base
        except Exception as exc:  # noqa: BLE001 - LLM 失败绝不阻塞，回落 Heuristic
            logger.warning("LlmStrategy 失败，回落 Heuristic - {}", exc)
            return base

    def _call_llm(
        self, inventory, profile, market, goal, base: StrategyDecision
    ) -> StrategyDecision | None:
        allowed_pb = [n for n in dcfg.playbook_names() if _playbook_feasibility(n, inventory) > 0.0]
        allowed_sp = list(inventory.ranked_selling_points) or [base.core_selling_point]
        if not allowed_pb:
            return None
        pos = (profile.positioning if profile else "") or ""
        aud = (profile.target_audience if profile else "") or ""
        system = (
            "You are a senior TikTok Shop video strategist. Decide HOW to sell a product in a "
            "short video. You must ONLY choose from the allowed playbooks and selling points. "
            "You do NOT write captions. Output strict JSON only."
        )
        user = (
            f"product: {inventory.product}\nmarket: {market}\ngoal: {goal}\n"
            f"positioning: {pos}\naudience: {aud}\n"
            f"available_selling_points (evidence-ranked): {allowed_sp}\n"
            f"available_shots: {sorted(inventory.shot_enum_counts.keys())}\n"
            f"allowed_playbooks: {allowed_pb}\n\n"
            'Return ONLY JSON: {"playbook": one of allowed_playbooks, '
            '"core_selling_point": one of available_selling_points, '
            '"angle": one short sentence, "emotion": "desire|trust|curiosity|urgency", '
            '"caption_style": short phrase, "supporting_points": [subset of selling points]}'
        )
        resp = httpx.post(
            f"{self.s.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
            json={
                "model": self.s.openai_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.7,
                "response_format": {"type": "json_object"},
            },
            timeout=40.0,
        )
        resp.raise_for_status()
        data = json.loads(resp.json()["choices"][0]["message"]["content"])

        playbook = str(data.get("playbook") or "").strip()
        core_sp = str(data.get("core_selling_point") or "").strip()
        if playbook not in allowed_pb:
            playbook = base.playbook
        if core_sp not in allowed_sp:
            core_sp = base.core_selling_point
        pb_cfg = dcfg.get_playbook(playbook)
        return StrategyDecision(
            playbook=playbook,
            core_selling_point=core_sp,
            angle=str(data.get("angle") or base.angle).strip()[:200],
            emotion=str(data.get("emotion") or pb_cfg.get("emotion") or base.emotion).strip(),
            tone="",
            caption_style=str(data.get("caption_style") or base.caption_style).strip()[:200],
            audio_mood=str(pb_cfg.get("audio_mood") or base.audio_mood),
            supporting_points=[
                str(x) for x in (data.get("supporting_points") or []) if str(x) in allowed_sp
            ][:3],
            scores=base.scores,
            rationale="LLM 策略（已白名单校验）",
        )
