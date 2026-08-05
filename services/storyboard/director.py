"""Conversion Director：剪辑前决定「卖哪个产品、只讲哪个核心卖点、镜头怎么排」，
产出固定四段 Storyboard（hook/value/proof/cta）。剪辑器只执行 Storyboard。

输入是选材引擎产出的 RenderPlan（有序片段+角色）。本模块不决定字幕文案，
只写「字幕意图」；具体文案由 SubtitleResolver 在渲染前按 market 解析。
"""

from __future__ import annotations

from core.enums import MaterialRole
from core.models import RenderPlan

from services.storyboard import config as sbcfg
from services.storyboard.models import Storyboard, StageSpec, SubtitleIntent

_STAGE_ORDER = ["hook", "value", "proof", "cta"]
_STAGE_ROLE = {
    "hook": MaterialRole.hook,
    "value": MaterialRole.value,
    "proof": MaterialRole.proof,
    "cta": MaterialRole.cta,
}


class ConversionDirector:
    def build(
        self,
        plan: RenderPlan,
        market: str,
        *,
        main_selling_point: str | None = None,
        selling_point_tags: list[str] | None = None,
        variant: str | None = None,
    ) -> Storyboard:
        structure = sbcfg.load_structure()
        shot_priority = sbcfg.load_shot_priority()
        target = str(structure.get("target") or "conversion")

        # 1) 选 variant：优先入参；否则按可用素材总时长与阈值决定 full/compact
        variant = variant or self._pick_variant(plan, structure)
        var_cfg = (structure.get("variants") or {}).get(variant) or {}
        stages_cfg = var_cfg.get("stages") or {}
        duration = int(var_cfg.get("total") or 0)

        # 2) 定核心卖点（一条视频只讲一个）
        sp = self._pick_selling_point(main_selling_point, selling_point_tags)

        # 3) 按角色把 clip 归入四段
        clips_by_role: dict[str, list[str]] = {s: [] for s in _STAGE_ORDER}
        for c in plan.clips:
            for stage_name, role in _STAGE_ROLE.items():
                if c.role_used == role:
                    clips_by_role[stage_name].append(c.record_id)
                    break

        # 4) 组装四段（顺序固定）
        structure_specs: list[StageSpec] = []
        for stage_name in _STAGE_ORDER:
            meta = stages_cfg.get(stage_name) or {}
            shot_meta = shot_priority.get(stage_name) or {}
            intent = SubtitleIntent(
                type=str(meta.get("intent_type") or ""),
                selling_point=sp,
                tone=str(meta.get("tone") or ""),
            )
            time_range = str(meta.get("time_range") or "")
            structure_specs.append(
                StageSpec(
                    stage=stage_name,
                    time_range=time_range,
                    goal=str(meta.get("goal") or ""),
                    shot_type=str(shot_meta.get("shot_type") or ""),
                    subtitle_intent=intent,
                    slot_sec=self._parse_slot_sec(time_range),
                    clip_record_ids=clips_by_role[stage_name],
                )
            )

        return Storyboard(
            product=plan.product_model,
            main_selling_point=sp,
            market=market,
            target=target,
            duration=duration,
            variant=variant,
            structure=structure_specs,
        )

    @staticmethod
    def _parse_slot_sec(time_range: str) -> float:
        """'0-3s' -> 3.0；'20-25s' -> 5.0。解析失败返回 0。"""
        try:
            s = time_range.strip().lower().replace("s", "")
            start, end = s.split("-")
            return max(0.0, float(end) - float(start))
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _pick_variant(plan: RenderPlan, structure: dict) -> str:
        threshold = float(structure.get("min_full_material_sec") or 18)
        return "full" if plan.total_duration_sec >= threshold else "compact"

    @staticmethod
    def _pick_selling_point(
        main_selling_point: str | None, selling_point_tags: list[str] | None
    ) -> str:
        if main_selling_point:
            return main_selling_point
        if selling_point_tags:
            return sbcfg.resolve_selling_point(*selling_point_tags)
        return sbcfg.default_selling_point()


_director = ConversionDirector()


def get_conversion_director() -> ConversionDirector:
    return _director
