"""选材引擎（成交导向 · 镜头级智能选材）。

规则：
- HOOK 开头、CTA 结尾各 1 条（必选）。
- 中间用 VALUE / PROOF 素材填充，构成 Hook→Value→Proof→CTA 成交结构。
- 同一成片内不重复用同一素材；N 条成片之间尽量不同组合。
- 选片不再纯随机：按「镜头优先级评分」（Layer 1，shot_priority + shot_type_mapping）
  为每个阶段挑最合适的镜头；一条视频只讲「证据最硬」的一个卖点（Layer 2）；
  稀缺的渔获/中鱼镜头优先留给 Hook/Proof 高潮（Layer 3）。
- 探索/利用：以 epsilon 概率随机探索（保证 N 条变体多样），否则按分利用。
"""

from __future__ import annotations

import random
from functools import lru_cache

from loguru import logger

from app.config import get_settings
from core.enums import MaterialRole
from core.models import Material, RenderClip, RenderPlan
from services.library import MaterialRepository, get_material_repository
from services.selection import scoring
from services.selection.performance import get_performance_store


class SelectionService:
    def __init__(
        self,
        repository: MaterialRepository,
        target_duration_sec: float,
        max_overshoot: float,
        explore_epsilon: float = 0.3,
    ) -> None:
        self.repository = repository
        self.target_duration_sec = target_duration_sec
        self.max_overshoot = max_overshoot
        self.explore_epsilon = explore_epsilon

    def producible_products(self) -> list[str]:
        """列出「有 HOOK 且有 CTA」因而能组片的产品型号（供批量生产/编排用）。"""
        materials = [
            m for m in self.repository.load_all() if m.onedrive_link and m.duration_sec > 0
        ]
        has_hook: set[str] = set()
        has_cta: set[str] = set()
        for m in materials:
            if not m.product_model:
                continue
            if m.has_role(MaterialRole.hook):
                has_hook.add(m.product_model)
            if m.has_role(MaterialRole.cta):
                has_cta.add(m.product_model)
        return sorted(has_hook & has_cta)

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
        values = [m for m in materials if m.has_role(MaterialRole.value)]
        proofs = [m for m in materials if m.has_role(MaterialRole.proof)]
        if not hooks or not ctas:
            raise ValueError(
                f"素材不足以组片：hooks={len(hooks)} ctas={len(ctas)}（product={product_model!r}）"
            )

        # Layer 5：接入成片表现回流（无数据则中性乐观，行为等同接入前）
        perf = get_performance_store()
        perf_opt = perf.opt_init
        mat_perf = (lambda m: perf.material_score(m.material_id)) if perf.has_data else None
        sp_perf = (lambda sp: perf.selling_point_score(sp)) if perf.has_data else None

        # Layer 2(+5)：卖点按「证据(+表现)」排序；多变体时轮换 top-K 卖点做 A/B 学习
        sp_ranked = scoring.rank_selling_points(materials, sp_perf, perf_opt)

        plans: list[RenderPlan] = []
        seen: set[tuple[str, ...]] = set()
        for i in range(count * 6):  # 多试几次以尽量凑出不同组合
            if len(plans) >= count:
                break
            # 变体轮换卖点：第 0 条用最优卖点；其余在 top-3 里轮换（探索不同卖点的成交表现）
            selling_point = sp_ranked[len(plans) % min(3, len(sp_ranked))] if count > 1 else sp_ranked[0]
            plan = self._build_one(
                hooks, ctas, values, proofs, selling_point, target, rng, mat_perf, perf_opt
            )
            if plan is None:
                continue
            sig = tuple(sorted(plan.material_ids))
            if sig in seen:
                continue
            seen.add(sig)
            plans.append(plan)

        if not plans:
            raise ValueError("未能组出任何成片计划，请检查素材角色/时长")
        logger.info(
            "选材完成 - product={} 计划数={} 卖点候选={} 表现回流={}",
            product_model, len(plans), sp_ranked[:3], "有" if perf.has_data else "无(纯结构分)",
        )
        return plans

    def plan_for_brief(self, brief, seed: int | None = None) -> RenderPlan:
        """Director 路径：按 Story Beats 逐段选镜头，执行 video_constraints 硬约束。

        - 每个 beat 用它的 roles 作候选池、shot_priority 打分、core_selling_point 对齐卖点；单镜头/拍。
        - 角色数量受 video_constraints.roles[min,max] 约束；单镜头时长封顶到 clip_max_duration。
        - 某拍找不到镜头 -> 自动调整（借用其它有余量的角色，或跳过该拍），**绝不失败**；
          并回写调整后的 beats（与 clips 对齐）+ 调整记录到 brief，供下游 compile/复盘。
        - 最后补齐角色下限（如 CTA 至少 1），并把 CTA 拍移到结尾。
        """
        import dataclasses as _dc

        from services.director.config import get_beat_def, load_video_constraints
        from services.director.models import Beat

        rng = random.Random(seed)
        materials = [
            m for m in self.repository.load_all() if m.onedrive_link and m.duration_sec > 0
        ]
        if brief.product:
            materials = [m for m in materials if m.product_model == brief.product]
        if not materials:
            raise ValueError(f"没有可用素材（product={brief.product!r}）")

        perf = get_performance_store()
        perf_opt = perf.opt_init
        mat_perf = (lambda m: perf.material_score(m.material_id)) if perf.has_data else None

        constraints = load_video_constraints()
        roles_cfg = constraints.get("roles") or {}
        role_max = {MaterialRole(k): int((v or {}).get("max", 99)) for k, v in roles_cfg.items()}
        role_min = {MaterialRole(k): int((v or {}).get("min", 0)) for k, v in roles_cfg.items()}
        clip_max_dur = float(constraints.get("clip_max_duration") or 10)
        all_roles = [MaterialRole.hook, MaterialRole.value, MaterialRole.proof, MaterialRole.cta]

        used: set[str] = set()
        role_count: dict[MaterialRole, int] = {r: 0 for r in all_roles}
        adjustments: list[str] = []
        pairs: list[tuple[Beat, RenderClip]] = []  # (最终 beat, 对应 clip) 一一对齐

        def _pick(role: MaterialRole, beat, explore: bool):
            if role_count.get(role, 0) >= role_max.get(role, 99):
                return None
            pool = [m for m in materials if m.has_role(role) and m.record_id not in used]
            # 取分数 top-K 候选，再优先选「够长」的（时长≥beat.min_sec），避免拿短镜头填长段被裁；
            # 都不够长则取候选里最长的（trim-only 下画面绝不循环/定格）。
            cands = scoring.rank_pick_n(
                pool, beat.name, beat.selling_point or brief.core_selling_point, 6, rng,
                epsilon=self.explore_epsilon if explore else 0.0, exclude=used,
                perf_fn=mat_perf, perf_opt=perf_opt, priority=beat.shot_priority,
            )
            if not cands:
                return None
            need = float(getattr(beat, "min_sec", 0.0) or 0.0)
            if need > 0:
                long_enough = [c for c in cands if float(c.duration_sec or 0.0) >= need]
                if long_enough:
                    return long_enough[0]
                return max(cands, key=lambda c: float(c.duration_sec or 0.0))
            return cands[0]

        def _dur(m: Material) -> float:
            return min(float(m.duration_sec or 0.0), clip_max_dur)

        def _target(dur: float, b) -> float:
            """口播/字幕预算时长 = 在 [min,max] 内、但绝不超过该镜头真实可用时长（不循环补时长）。"""
            hi = float(getattr(b, "max_sec", 0.0) or 0.0)
            lo = float(getattr(b, "min_sec", 0.0) or 0.0)
            t = dur
            if hi > 0:
                t = min(t, hi)
            if lo > 0 and t < lo:
                t = min(lo, dur)
            return round(max(0.5, t), 1)

        for beat in brief.beats:
            target_roles = list(beat.roles) or [MaterialRole.value]
            chosen_role, m = None, None
            for role in target_roles:
                m = _pick(role, beat, explore=True)
                if m is not None:
                    chosen_role = role
                    break
            if m is None:  # 目标角色都没镜头/已满 -> 借其它有余量的角色
                for role in all_roles:
                    if role in target_roles:
                        continue
                    m = _pick(role, beat, explore=False)
                    if m is not None:
                        chosen_role = role
                        adjustments.append(
                            f"beat '{beat.name}': 目标角色无镜头，改用 {role.value}"
                        )
                        break
            if m is None:
                adjustments.append(f"beat '{beat.name}': 无可用镜头，已跳过该拍")
                continue
            used.add(m.record_id)
            role_count[chosen_role] += 1
            dur = _dur(m)
            fixed = _dc.replace(beat, roles=[chosen_role], slot_sec=_target(dur, beat))
            pairs.append((fixed, self._clip(m, chosen_role, beat=beat.name, duration=dur)))

        # 补齐角色下限（如 CTA>=1 / VALUE>=1）：缺则挑最合适的补一拍
        for role, mn in role_min.items():
            while role_count.get(role, 0) < mn:
                pool = [m for m in materials if m.has_role(role) and m.record_id not in used]
                got = scoring.rank_pick_n(
                    pool, role.value.lower(), brief.core_selling_point, 1, rng,
                    epsilon=0.0, perf_fn=mat_perf, perf_opt=perf_opt,
                )
                if not got:
                    adjustments.append(f"角色 {role.value} 少于下限 {mn}，且无更多镜头可补")
                    break
                m = got[0]
                used.add(m.record_id)
                role_count[role] += 1
                dur = _dur(m)
                bd = get_beat_def(role.value.lower())
                name = role.value.lower()
                syn = Beat(
                    name=name, purpose=str(bd.get("goal") or name),
                    roles=[role], intent_type=name, selling_point=brief.core_selling_point,
                    tone=str(bd.get("tone") or ""),
                    shot_priority=list(bd.get("shots") or []),
                    min_sec=float(bd.get("min_sec") or 0.0),
                    max_sec=float(bd.get("max_sec") or 4.0),
                )
                syn.slot_sec = _target(dur, syn)
                pairs.append((syn, self._clip(m, role, beat=name, duration=dur)))
                adjustments.append(f"补齐角色 {role.value}（下限 {mn}），新增一拍 '{name}'")

        if not pairs:
            raise ValueError("未能按 Brief 选出任何镜头，请检查素材角色/时长")

        # CTA 收尾：把 CTA 角色的拍稳定地移到最后（保持其余相对顺序）
        pairs.sort(key=lambda p: 1 if MaterialRole.cta in p[0].roles else 0)

        # 去重 beat 名（compile 按 beat 名分组），并回写对齐后的 beats + 调整记录
        seen_names: dict[str, int] = {}
        beats_out: list[Beat] = []
        clips: list[RenderClip] = []
        for b, c in pairs:
            nm = b.name
            if nm in seen_names:
                seen_names[nm] += 1
                nm = f"{b.name}_{seen_names[nm]}"
                b = _dc.replace(b, name=nm)  # Beat 是 dataclass
                c = c.model_copy(update={"beat": nm})  # RenderClip 是 Pydantic BaseModel
            else:
                seen_names[nm] = 1
            beats_out.append(b)
            clips.append(c)

        brief.beats = beats_out
        brief.adjustments = adjustments
        if adjustments:
            brief.needs_review = True
            logger.info("Brief 选材自动调整 {} 处 - {}", len(adjustments), "; ".join(adjustments))

        logger.info(
            "Brief 选材完成 - product={} source={} beats={} clips={} roles={} sp={}",
            brief.product, brief.source, len(beats_out), len(clips),
            {r.value: c for r, c in role_count.items() if c}, brief.core_selling_point,
        )
        return RenderPlan(
            product_model=brief.product,
            clips=clips,
            target_duration_sec=float(brief.duration or self.target_duration_sec),
            selling_point=brief.core_selling_point,
            playbook=brief.playbook,
        )

    @staticmethod
    def _role_for_beat(material: Material, roles: list[MaterialRole]) -> MaterialRole:
        """该 clip 在 beat 里的标注角色：取素材实际具备且 beat 接受的第一个角色；借片时回落。"""
        for r in roles:
            if material.has_role(r):
                return r
        if roles:
            return roles[0]
        return material.roles[0] if material.roles else MaterialRole.value

    def _build_one(
        self,
        hooks: list[Material],
        ctas: list[Material],
        values: list[Material],
        proofs: list[Material],
        selling_point: str,
        target: float,
        rng: random.Random,
        perf_fn=None,
        perf_opt: float = 0.0,
    ) -> RenderPlan | None:
        eps = self.explore_epsilon
        # Layer 1+3+4：Hook 取最强钩子镜头（动作/中鱼/抛投优先），并多取 2-3 条做「快切」。
        # 第一条即最高分 => 首帧就是峰值动作；不足则退回单条。
        pf = {"perf_fn": perf_fn, "perf_opt": perf_opt}
        hook_sel = scoring.rank_pick_n(hooks, "hook", selling_point, 3, rng, epsilon=eps, **pf)
        if not hook_sel:
            return None
        used = {m.record_id for m in hook_sel}
        # CTA 取产品美镜/渔获收尾
        cta = scoring.rank_pick(ctas, "cta", selling_point, rng, epsilon=eps, exclude=used, **pf)
        if cta is None:
            return None
        used.add(cta.record_id)

        # Value 1-2 条：核心卖点匹配 / 操作明显；Proof 2-3 条：实战/测试/多角度
        value_sel = scoring.rank_pick_n(
            values, "value", selling_point, 2, rng, epsilon=eps, exclude=used, **pf
        )
        used.update(m.record_id for m in value_sel)
        proof_sel = scoring.rank_pick_n(
            proofs, "proof", selling_point, 3, rng, epsilon=eps, exclude=used, **pf
        )
        used.update(m.record_id for m in proof_sel)

        # 兜底：若中段一条都没有（value/proof 角色缺失），退回用另一类补，保证结构不空
        if not value_sel and not proof_sel:
            filler = scoring.rank_pick_n(
                [m for m in (values + proofs)], "proof", selling_point, 2, rng,
                epsilon=eps, exclude=used, **pf,
            )
            proof_sel = filler

        clips = [self._clip(m, MaterialRole.hook) for m in hook_sel]
        clips += [self._clip(m, MaterialRole.value) for m in value_sel]
        clips += [self._clip(m, MaterialRole.proof) for m in proof_sel]
        clips.append(self._clip(cta, MaterialRole.cta))

        return RenderPlan(
            product_model=hook_sel[0].product_model,
            clips=clips,
            target_duration_sec=target,
            selling_point=selling_point,
        )

    @staticmethod
    def _clip(
        material: Material, role: MaterialRole, beat: str = "", duration: float | None = None
    ) -> RenderClip:
        return RenderClip(
            record_id=material.record_id,
            material_id=material.material_id,
            role_used=role,
            onedrive_link=material.onedrive_link,
            duration_sec=material.duration_sec if duration is None else duration,
            keep_original=material.keep_original_audio,
            beat=beat,
        )


@lru_cache
def get_selection_service() -> SelectionService:
    s = get_settings()
    return SelectionService(
        repository=get_material_repository(),
        target_duration_sec=s.selection_target_duration_sec,
        max_overshoot=s.selection_max_overshoot,
        explore_epsilon=s.selection_epsilon_start,
    )
