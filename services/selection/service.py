"""选材引擎（flexible 策略）。

规则：
- HOOK 开头、CTA 结尾各 1 条（必选）。
- 中间用 VALUE / PROOF 素材按目标时长凑，直到接近目标（允许一定超出）。
- 同一成片内不重复用同一素材；N 条成片之间尽量不同组合。
- 阶段 2 暂无素材评分，选材用均匀随机（即纯探索）；阶段 6 接入评分后，
  这里改成“ε 探索 + 按分利用”，接口不变。
"""

from __future__ import annotations

import random
from functools import lru_cache

from loguru import logger

from app.config import get_settings
from core.enums import MaterialRole
from core.models import Material, RenderClip, RenderPlan
from services.library import MaterialRepository, get_material_repository


class SelectionService:
    def __init__(
        self,
        repository: MaterialRepository,
        target_duration_sec: float,
        max_overshoot: float,
    ) -> None:
        self.repository = repository
        self.target_duration_sec = target_duration_sec
        self.max_overshoot = max_overshoot

    def plan(
        self,
        product_model: str,
        count: int = 1,
        target_duration_sec: float | None = None,
        seed: int | None = None,
    ) -> list[RenderPlan]:
        target = target_duration_sec or self.target_duration_sec
        rng = random.Random(seed)
        materials = [m for m in self.repository.load_all() if m.onedrive_link and m.duration_sec > 0]
        if product_model:
            materials = [m for m in materials if m.product_model == product_model]
        if not materials:
            raise ValueError(f"没有可用素材（product={product_model!r}）")

        hooks = [m for m in materials if m.has_role(MaterialRole.hook)]
        ctas = [m for m in materials if m.has_role(MaterialRole.cta)]
        middles = [
            m for m in materials
            if m.has_role(MaterialRole.value) or m.has_role(MaterialRole.proof)
        ]
        if not hooks or not ctas:
            raise ValueError(
                f"素材不足以组片：hooks={len(hooks)} ctas={len(ctas)}（product={product_model!r}）"
            )

        plans: list[RenderPlan] = []
        seen: set[tuple[str, ...]] = set()
        for _ in range(count * 5):  # 多试几次以尽量凑出不同组合
            if len(plans) >= count:
                break
            plan = self._build_one(hooks, ctas, middles, target, rng)
            if plan is None:
                continue
            sig = tuple(sorted(plan.material_ids))
            if sig in seen:
                continue
            seen.add(sig)
            plans.append(plan)

        if not plans:
            raise ValueError("未能组出任何成片计划，请检查素材角色/时长")
        logger.info("选材完成 - product={} 计划数={}", product_model, len(plans))
        return plans

    def _build_one(
        self,
        hooks: list[Material],
        ctas: list[Material],
        middles: list[Material],
        target: float,
        rng: random.Random,
    ) -> RenderPlan | None:
        hook = rng.choice(hooks)
        cta = rng.choice([c for c in ctas if c.record_id != hook.record_id] or ctas)

        used = {hook.record_id, cta.record_id}
        budget = target - hook.duration_sec - cta.duration_sec
        hard_cap = target * self.max_overshoot - hook.duration_sec - cta.duration_sec

        pool = [m for m in middles if m.record_id not in used]
        rng.shuffle(pool)
        chosen_middles: list[Material] = []
        acc = 0.0
        for m in pool:
            if acc >= budget:
                break
            if acc + m.duration_sec > hard_cap and chosen_middles:
                continue
            chosen_middles.append(m)
            used.add(m.record_id)
            acc += m.duration_sec

        clips = [self._clip(hook, MaterialRole.hook)]
        for m in chosen_middles:
            role = MaterialRole.value if m.has_role(MaterialRole.value) else MaterialRole.proof
            clips.append(self._clip(m, role))
        clips.append(self._clip(cta, MaterialRole.cta))

        return RenderPlan(
            product_model=hook.product_model,
            clips=clips,
            target_duration_sec=target,
        )

    @staticmethod
    def _clip(material: Material, role: MaterialRole) -> RenderClip:
        return RenderClip(
            record_id=material.record_id,
            material_id=material.material_id,
            role_used=role,
            onedrive_link=material.onedrive_link,
            duration_sec=material.duration_sec,
            keep_original=material.keep_original_audio,
        )


@lru_cache
def get_selection_service() -> SelectionService:
    s = get_settings()
    return SelectionService(
        repository=get_material_repository(),
        target_duration_sec=s.selection_target_duration_sec,
        max_overshoot=s.selection_max_overshoot,
    )
