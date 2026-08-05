"""镜头级选材评分（Layer 1）+ 卖点×证据判定（Layer 2）。

- 把飞书「素材类型」经 shot_type_mapping.yaml 归一到 shot_priority.yaml 的镜头枚举；
- 按阶段 priority 给候选素材打分，越靠前的镜头得分越高；
- 卖点匹配的素材额外加分（value/proof 更愿意用能证明该卖点的镜头）。
所有规则都在 config 里，代码不写死。命中不到只是记 0 分，绝不阻塞选材。
"""

from __future__ import annotations

import random
from functools import lru_cache
from pathlib import Path

import yaml
from loguru import logger

from core.enums import MaterialRole
from core.models import Material
from services.storyboard import config as sbcfg

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# 证明力强的镜头枚举（用于 Layer 2 评估「某卖点的证据是否够硬」，以及稀缺镜头识别）
_PROOF_ENUMS = {"real_use", "test_shot", "pulling_fish", "catch", "strong_action", "comparison"}
# 稀缺「渔获/中鱼」类镜头枚举（Layer 3 优先留给 hook/proof 高潮）
SCARCE_ENUMS = {"catch", "pulling_fish"}


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 yaml 失败({})：{}", path, exc)
        return {}


@lru_cache(maxsize=1)
def _shot_type_mapping() -> dict[str, list[str]]:
    raw = (_load_yaml(CONFIG_DIR / "shot_type_mapping.yaml").get("mapping") or {})
    return {str(k).strip().lower(): [str(x) for x in (v or [])] for k, v in raw.items()}


@lru_cache(maxsize=1)
def _shot_priority() -> dict:
    return _load_yaml(CONFIG_DIR / "shot_priority.yaml")


def material_selling_point(m: Material) -> str:
    """素材归一后的英文卖点（主标签 + 辅助标签）。"""
    tags = ([m.main_tag] if m.main_tag else []) + list(m.aux_tags or [])
    return sbcfg.resolve_selling_point(*tags)


def shot_enums(m: Material, selling_point: str = "") -> set[str]:
    """素材覆盖的镜头枚举：素材类型映射 + 卖点匹配时补 core_point_match/point_clear。"""
    enums: set[str] = set()
    mt = (m.material_type or "").strip().lower()
    if mt:
        enums.update(_shot_type_mapping().get(mt, []))
        # 子串兜底：如「河边作钓」未精确命中时匹配包含关系
        if not enums:
            for key, vals in _shot_type_mapping().items():
                if key and key in mt:
                    enums.update(vals)
                    break
    if selling_point and material_selling_point(m) == selling_point:
        enums.update({"core_point_match", "point_clear"})
    return enums


def stage_score(
    m: Material, stage: str, selling_point: str = "", priority: list[str] | None = None
) -> float:
    """素材在某 stage 的镜头得分：覆盖枚举在优先级列表里的最高名次；卖点对齐再 +0.5。

    priority 显式传入时用它（Director beat 自带 shot_priority）；否则回落 shot_priority.yaml 的 stage。
    """
    prio = list(priority) if priority is not None else list(
        (_shot_priority().get(stage) or {}).get("priority") or []
    )
    if not prio:
        return 0.0
    enums = shot_enums(m, selling_point)
    best = None
    for i, key in enumerate(prio):
        if key in enums:
            best = i
            break
    base = float(len(prio) - best) if best is not None else 0.0
    if selling_point and material_selling_point(m) == selling_point:
        base += 0.5
    return base


def is_scarce(m: Material) -> bool:
    """是否稀缺「渔获/中鱼」镜头（Layer 3 调度用）。"""
    return bool(shot_enums(m) & SCARCE_ENUMS)


def evidence_weights(materials: list[Material]) -> dict[str, float]:
    """各卖点的证据权重：证明力强的素材权重 2，其余 1。"""
    weights: dict[str, float] = {}
    for m in materials:
        sp = material_selling_point(m)
        if not sp:
            continue
        w = 2.0 if (shot_enums(m) & _PROOF_ENUMS) else 1.0
        weights[sp] = weights.get(sp, 0.0) + w
    return weights


def rank_selling_points(materials: list[Material], sp_perf_fn=None, perf_opt: float = 0.0) -> list[str]:
    """按「证据权重(归一) + 表现分」降序排列卖点。冷启动无表现数据时纯按证据。"""
    weights = evidence_weights(materials)
    if not weights:
        return [sbcfg.default_selling_point()]
    mx = max(weights.values()) or 1.0
    scored: list[tuple[float, str]] = []
    for sp, w in weights.items():
        ev = w / mx  # 归一到 0~1，与表现分同量纲（证据 = 冷启动先验）
        # 表现分随样本累积可越过证据先验，实现「数据够了就让高成交卖点胜出」
        perf = PERF_INFLUENCE * (float(sp_perf_fn(sp)) - perf_opt) if sp_perf_fn is not None else 0.0
        scored.append((ev + perf, sp))
    scored.sort(reverse=True)
    return [sp for _, sp in scored]


def pick_selling_point_by_evidence(materials: list[Material], sp_perf_fn=None, perf_opt: float = 0.0) -> str:
    """Layer 2：选「证据最硬（+表现最好）」的卖点。"""
    return rank_selling_points(materials, sp_perf_fn, perf_opt)[0]


# 表现分对结构分的影响幅度：一个远超预期的爆款镜头最多可跨越约 1 个优先级名次。
PERF_INFLUENCE = 2.0


def _combined_score(
    m: Material,
    stage: str,
    selling_point: str,
    perf_fn=None,
    perf_opt: float = 0.0,
    priority: list[str] | None = None,
) -> float:
    """Layer1 结构分 + Layer5 表现分：score = shot_score + 2·(perf - opt)。

    perf 高于乐观初值(opt)的镜头被抬升、低于的被压低；未见过的镜头 perf≈opt，调整为 0 =>
    退化为纯结构分。冷启动/无数据时行为与接入表现回流前完全一致。
    """
    base = stage_score(m, stage, selling_point, priority)
    if perf_fn is not None:
        base += PERF_INFLUENCE * (float(perf_fn(m)) - perf_opt)
    return base


def rank_pick(
    candidates: list[Material],
    stage: str,
    selling_point: str,
    rng: random.Random,
    *,
    epsilon: float = 0.0,
    exclude: set[str] | None = None,
    perf_fn=None,
    perf_opt: float = 0.0,
    priority: list[str] | None = None,
) -> Material | None:
    """按「结构分+表现分」选一条：概率 epsilon 随机探索，否则取最高分（同分随机）。"""
    pool = [m for m in candidates if not exclude or m.record_id not in exclude]
    if not pool:
        return None
    if epsilon > 0 and rng.random() < epsilon:
        return rng.choice(pool)
    scored = [
        (_combined_score(m, stage, selling_point, perf_fn, perf_opt, priority), rng.random(), m)
        for m in pool
    ]
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


def rank_pick_n(
    candidates: list[Material],
    stage: str,
    selling_point: str,
    n: int,
    rng: random.Random,
    *,
    epsilon: float = 0.0,
    exclude: set[str] | None = None,
    perf_fn=None,
    perf_opt: float = 0.0,
    priority: list[str] | None = None,
) -> list[Material]:
    """按「结构分+表现分」取前 n 条（带 epsilon 探索，去重）。"""
    used = set(exclude or set())
    out: list[Material] = []
    for _ in range(max(0, n)):
        pick = rank_pick(
            candidates, stage, selling_point, rng,
            epsilon=epsilon, exclude=used, perf_fn=perf_fn, perf_opt=perf_opt, priority=priority,
        )
        if pick is None:
            break
        out.append(pick)
        used.add(pick.record_id)
    return out
