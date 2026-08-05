"""DirectorEngine：整个视频生成系统的「大脑」，在所有模块之前运行。

专业导演工作流：
  产品任务 -> 产品能力盘点(MaterialInventory)
           -> Creative Brief + Story Beats（LLM 自由编排，不写死结构，不含最终字幕）
           -> (下游) Selection 选镜头 -> Caption 结合镜头写字幕 -> compile -> 配音 -> 剪辑 -> 渲染

LLM 不可用/失败时回落启发式（config/playbooks.yaml 的结构库）。所有产出都受 video_constraints 约束。
"""

from __future__ import annotations

from functools import lru_cache

from loguru import logger

from app.config import get_settings
from core.enums import MaterialRole
from services.director import config as dcfg
from services.director import llm as director_llm
from services.director.models import Beat, Brief, MaterialInventory
from services.director.strategy import HeuristicStrategy
from services.library import (
    MaterialRepository,
    ProductRepository,
    get_material_repository,
    get_product_repository,
)
from services.selection import scoring
from services.selection.performance import get_performance_store

# 有 prompt_library 支持的字幕意图（beat.name 命不中时按角色回落到这些）
_ALLOWED_INTENTS = {"hook", "value", "proof", "cta", "question", "demo", "problem", "solution"}
_ROLE_DEFAULT_INTENT = {
    MaterialRole.hook: "hook",
    MaterialRole.value: "value",
    MaterialRole.proof: "proof",
    MaterialRole.cta: "cta",
}


def _role_from_str(name: str) -> MaterialRole | None:
    try:
        return MaterialRole(str(name).strip().upper())
    except Exception:  # noqa: BLE001
        return None


def _infer_role(name: str) -> MaterialRole:
    n = (name or "").lower()
    if any(k in n for k in ("cta", "buy", "purchase", "order", "shop")):
        return MaterialRole.cta
    if any(k in n for k in ("proof", "test", "trust", "result", "solution", "review")):
        return MaterialRole.proof
    if any(k in n for k in ("hook", "question", "problem", "pain", "curios")):
        return MaterialRole.hook
    return MaterialRole.value


def _intent_for(name: str, role: MaterialRole) -> str:
    n = (name or "").lower()
    if n in _ALLOWED_INTENTS:
        return n
    for key, intent in (
        ("quest", "question"), ("problem", "problem"), ("pain", "problem"),
        ("solution", "solution"), ("fix", "solution"), ("demo", "demo"), ("show", "demo"),
        ("proof", "proof"), ("test", "proof"), ("trust", "proof"),
        ("cta", "cta"), ("buy", "cta"), ("purchase", "cta"),
        ("hook", "hook"), ("value", "value"), ("benefit", "value"),
    ):
        if key in n:
            return intent
    return _ROLE_DEFAULT_INTENT.get(role, "value")


class DirectorEngine:
    def __init__(
        self,
        repository: MaterialRepository,
        product_repository: ProductRepository | None,
        settings,
    ) -> None:
        self.repository = repository
        self.product_repository = product_repository
        self.s = settings

    # ---- inventory -------------------------------------------------------
    def build_inventory(self, product_model: str) -> MaterialInventory:
        materials = [
            m
            for m in self.repository.load_all()
            if m.onedrive_link and m.duration_sec > 0
            and (not product_model or m.product_model == product_model)
        ]
        perf = get_performance_store()
        sp_perf = (lambda sp: perf.selling_point_score(sp)) if perf.has_data else None

        role_counts: dict[str, int] = {}
        shot_counts: dict[str, int] = {}
        total = 0.0
        for m in materials:
            total += float(m.duration_sec or 0.0)
            for r in m.roles:
                role_counts[r.value] = role_counts.get(r.value, 0) + 1
            for e in scoring.shot_enums(m):
                shot_counts[e] = shot_counts.get(e, 0) + 1

        weights = scoring.evidence_weights(materials)
        ranked = scoring.rank_selling_points(materials, sp_perf, perf.opt_init)

        return MaterialInventory(
            product=product_model,
            total_available_sec=total,
            role_counts=role_counts,
            selling_point_weights=weights,
            ranked_selling_points=ranked,
            shot_enum_counts=shot_counts,
            material_count=len(materials),
        )

    # ---- brief -----------------------------------------------------------
    def plan(
        self,
        product_model: str,
        market: str,
        goal: str = "conversion",
        *,
        inventory: MaterialInventory | None = None,
    ) -> Brief:
        inv = inventory or self.build_inventory(product_model)
        profile = self.product_repository.get(product_model) if self.product_repository else None
        constraints = dcfg.load_video_constraints()

        story, source = None, "heuristic"
        if self.s.director_strategy == "llm":
            story = director_llm.generate_story(inv, profile, market, goal, constraints, self.s)
            if story:
                source = "llm"
        if story is None:
            story = self._heuristic_story(inv, profile, market, goal)

        beats = self._normalize_beats(story, inv, constraints)
        beats = self._finalize_durations(beats, constraints)

        core_sp = self._valid_sp(story.get("core_selling_point"), inv)
        total = int(round(sum(b.slot_sec for b in beats)))
        brief = Brief(
            product=product_model,
            market=market,
            country=self.s.content_country,
            goal=goal,
            angle=str(story.get("angle") or ""),
            core_selling_point=core_sp,
            supporting_points=[
                s for s in (story.get("supporting_points") or []) if s in inv.ranked_selling_points
            ][:3],
            emotion=str(story.get("emotion") or "desire"),
            tone=str(story.get("tone") or ""),
            caption_style=str(story.get("caption_style") or ""),
            audio_mood=str(story.get("audio_mood") or "energetic"),
            playbook=str(story.get("playbook") or ("llm" if source == "llm" else "heuristic")),
            duration=total,
            variant=str(story.get("variant") or ""),
            beats=beats,
            rationale=str(story.get("rationale") or ""),
            scores=story.get("scores") or {},
            source=source,
        )
        logger.info(
            "Director Brief[{}] - product={} market={} core_sp={} emotion={} beats=[{}] ~{}s",
            source, product_model, market, brief.core_selling_point, brief.emotion,
            ", ".join(f"{b.name}({b.min_sec:g}-{b.max_sec:g}s)" for b in beats), total,
        )
        return brief

    # ---- helpers ---------------------------------------------------------
    def _valid_sp(self, raw, inv: MaterialInventory) -> str:
        raw = str(raw or "").strip()
        if raw and raw in inv.ranked_selling_points:
            return raw
        return inv.ranked_selling_points[0] if inv.ranked_selling_points else raw

    def _heuristic_story(self, inv, profile, market, goal) -> dict:
        """LLM 不可用时的启发式出稿：用 config/playbooks.yaml 的结构库拼 beats。"""
        strat = HeuristicStrategy(
            perf=get_performance_store(), epsilon=self.s.selection_epsilon_start,
            perf_opt=self.s.scoring_optimistic_init,
        )
        decision = strat.decide(inv, profile, market, goal)
        beat_names = dcfg.get_playbook(decision.playbook).get("beats") or ["hook", "value", "proof", "cta"]
        beats = []
        for name in beat_names:
            bd = dcfg.get_beat_def(name)
            beats.append({
                "name": name,
                "purpose": str(bd.get("goal") or name),
                "express": "",
                "emotion": decision.emotion,
                "role": (bd.get("roles") or ["VALUE"])[0],
                "shots": list(bd.get("shots") or []),
                "min_sec": float(bd.get("min_sec") or 0.0),
                "max_sec": float(bd.get("max_sec") or float(bd.get("weight") or 3)),
            })
        return {
            "angle": decision.angle,
            "core_selling_point": decision.core_selling_point,
            "supporting_points": decision.supporting_points,
            "emotion": decision.emotion,
            "tone": decision.tone,
            "caption_style": decision.caption_style,
            "audio_mood": decision.audio_mood,
            "rationale": decision.rationale,
            "playbook": decision.playbook,
            "scores": decision.scores,
            "beats": beats,
        }

    def _normalize_beats(self, story: dict, inv: MaterialInventory, constraints: dict) -> list[Beat]:
        """把 story.beats 规范成 Beat：角色/意图/镜头/时长归一，并按 role max 限制 beat 数量。"""
        core_sp = self._valid_sp(story.get("core_selling_point"), inv)
        clip_max = float(constraints.get("clip_max_duration") or 10)
        role_max = {
            r: int((v or {}).get("max", 99))
            for r, v in (constraints.get("roles") or {}).items()
        }

        beats: list[Beat] = []
        role_used: dict[str, int] = {}
        has_cta = False
        for raw in (story.get("beats") or []):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "beat").strip() or "beat"
            role = _role_from_str(raw.get("role")) or _infer_role(name)
            # 按 role max 限制 beat 数量（超出的直接跳过，避免 selection 阶段浪费）
            if role_used.get(role.value, 0) >= role_max.get(role.value, 99):
                continue
            role_used[role.value] = role_used.get(role.value, 0) + 1
            if role == MaterialRole.cta:
                has_cta = True

            mn = max(0.0, float(raw.get("min_sec") or 0.0))
            mx = float(raw.get("max_sec") or 0.0)
            if mx <= 0:
                mx = max(mn, 3.0)
            mx = min(mx, clip_max)
            mn = min(mn, mx)
            intent = _intent_for(name, role)
            beats.append(Beat(
                name=name,
                purpose=str(raw.get("purpose") or ""),
                express=str(raw.get("express") or ""),
                emotion=str(raw.get("emotion") or story.get("emotion") or ""),
                roles=[role],
                intent_type=intent,
                selling_point=core_sp,
                tone=str(raw.get("tone") or ""),
                goal=str(raw.get("purpose") or intent),
                shot_priority=[str(s).strip() for s in (raw.get("shots") or []) if str(s).strip()],
                min_sec=mn,
                max_sec=mx,
            ))

        if not beats:
            beats.append(Beat(name="hook", roles=[MaterialRole.hook], intent_type="hook",
                              selling_point=core_sp, min_sec=1, max_sec=2))
        # 保证有 CTA（role min）：缺则补一个收尾
        if not has_cta and role_max.get("CTA", 1) >= 1:
            beats.append(Beat(name="cta", purpose="drive purchase", roles=[MaterialRole.cta],
                              intent_type="cta", selling_point=core_sp, tone="urgent",
                              shot_priority=list(dcfg.get_beat_def("cta").get("shots") or []),
                              min_sec=2, max_sec=5))
        return beats

    def _finalize_durations(self, beats: list[Beat], constraints: dict) -> list[Beat]:
        """把 beat 建议时长收敛到 video_duration 总区间，回填 slot_sec(文案目标)/time_range。"""
        vdur = constraints.get("video_duration") or {}
        vmin, vmax = float(vdur.get("min") or 15), float(vdur.get("max") or 30)
        clip_max = float(constraints.get("clip_max_duration") or 10)

        targets = [max(b.min_sec, (b.min_sec + b.max_sec) / 2.0 if b.max_sec else b.min_sec or 3.0)
                   for b in beats]
        total = sum(targets) or 1.0
        if total > vmax:
            scale = vmax / total
            targets = [max(b.min_sec, t * scale) for b, t in zip(beats, targets)]
        elif total < vmin:
            scale = vmin / total
            targets = [min(clip_max, t * scale) for t in targets]

        acc = 0.0
        for b, t in zip(beats, targets):
            b.slot_sec = round(min(max(t, 0.5), clip_max), 1)
            start = round(acc)
            acc += b.slot_sec
            b.time_range = f"{start}-{round(acc)}s"
        return beats


@lru_cache
def get_director_engine() -> DirectorEngine:
    s = get_settings()
    return DirectorEngine(
        repository=get_material_repository(),
        product_repository=get_product_repository(),
        settings=s,
    )
