"""Layer 5：成片表现回流存储 + 反馈评分（CTR/CVR/GMV → 选材「按分利用」）。

数据来源：data/performance.jsonl，每行一条成片表现（发布后由飞书/平台回流写入）：
    {
      "render": "S2_...mp4", "product": "S2", "selling_point": "strong_pull",
      "material_ids": ["mat_a","mat_b",...],
      "completion": 0.42, "engagement": 0.08, "views": 0.30, "gmv": 0.55   # 均为 0~1 归一值
    }
未归一的原始量（播放数/成交额）请上游先归一到 0~1（同产品内 min-max 或分位）。

评分：
- 复合回报 reward = Σ 权重×指标（权重取 config.perf_weight_*）。
- 贝叶斯收缩：score = (Σreward + k·opt) / (n + k)；k=scoring_shrink_k, opt=scoring_optimistic_init。
  样本少时向乐观初值收缩 => 冷启动鼓励探索；样本多时收敛到经验均值 => 按分利用。
- 无任何数据时全部返回 opt（中性乐观），选材退化为纯 Layer1 结构分 + ε 探索，行为与接入前一致。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from loguru import logger

from app.config import get_settings

PERF_PATH = Path("data/performance.jsonl")


class PerformanceStore:
    def __init__(self, path: Path, weights: dict[str, float], shrink_k: float, opt_init: float) -> None:
        self.path = Path(path)
        self.weights = weights
        self.shrink_k = float(shrink_k)
        self.opt_init = float(opt_init)
        self._mat: dict[str, list[float]] = {}   # material_id -> [sum_reward, n]
        self._sp: dict[str, list[float]] = {}     # selling_point -> [sum_reward, n]
        self._pb: dict[str, list[float]] = {}     # playbook -> [sum_reward, n]（Director 学习用）
        self._bgm: dict[str, list[float]] = {}    # bgm_track(provider:id) -> [sum_reward, n]（BGM 按 GMV 复用）
        self._loaded = False

    def _reward(self, rec: dict) -> float:
        """复合回报（0~1）：以 GMV + 转化率(CVR) 为主，商品点击率(CTR)、播放为辅。

        各分量为「同产品内分位归一」后的 0~1 值（由日更评分脚本计算后写入）。
        兼容旧字段：completion/engagement 若存在仍按权重计入（默认权重 0）。
        """
        w = self.weights

        def g(*keys: str) -> float:
            for k in keys:
                v = rec.get(k)
                if v is not None:
                    return float(v or 0.0)
            return 0.0

        r = (
            w.get("gmv", 0.0) * g("gmv")
            + w.get("cvr", 0.0) * g("cvr")
            + w.get("ctr", 0.0) * g("ctr", "engagement")
            + w.get("views", 0.0) * g("views")
            + w.get("completion", 0.0) * g("completion")
        )
        return max(0.0, min(1.0, r))

    def load(self) -> None:
        self._mat.clear()
        self._sp.clear()
        self._pb.clear()
        self._bgm.clear()
        # 指标每天更新、同一成片会多次回流（GMV 是累计值）：按 render 去重，只保留「最新快照」，
        # 避免同一条成片被反复累加造成重复计数。无 render 标识的旧记录各自独立保留。
        by_render: dict[str, dict] = {}
        order: list[str] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                key = str(rec.get("render") or f"__noid_{len(order)}")
                if key not in by_render:
                    order.append(key)
                by_render[key] = rec  # 后出现的覆盖先前的 => 取最新快照
        for key in order:
            rec = by_render[key]
            reward = self._reward(rec)
            for mid in rec.get("material_ids") or []:
                slot = self._mat.setdefault(str(mid), [0.0, 0.0])
                slot[0] += reward
                slot[1] += 1
            sp = rec.get("selling_point")
            if sp:
                slot = self._sp.setdefault(str(sp), [0.0, 0.0])
                slot[0] += reward
                slot[1] += 1
            pb = rec.get("playbook")
            if pb:
                slot = self._pb.setdefault(str(pb), [0.0, 0.0])
                slot[0] += reward
                slot[1] += 1
            bt = rec.get("bgm_track")
            if bt:
                slot = self._bgm.setdefault(str(bt), [0.0, 0.0])
                slot[0] += reward
                slot[1] += 1
        if order:
            logger.info("表现回流加载 {} 条(去重后) - 覆盖素材{} 卖点{} 结构{}",
                        len(order), len(self._mat), len(self._sp), len(self._pb))
        self._loaded = True

    def _shrunk(self, agg: dict[str, list[float]], key: str) -> float:
        if not self._loaded:
            self.load()
        s, n = agg.get(key, [0.0, 0.0])
        return (s + self.shrink_k * self.opt_init) / (n + self.shrink_k) if (n + self.shrink_k) else self.opt_init

    def material_score(self, material_id: str) -> float:
        return self._shrunk(self._mat, str(material_id))

    def selling_point_score(self, sp: str) -> float:
        return self._shrunk(self._sp, str(sp))

    def playbook_score(self, playbook: str) -> float:
        return self._shrunk(self._pb, str(playbook))

    def bgm_score(self, bgm_track: str) -> float:
        """BGM 曲目按 GMV/表现的贝叶斯收缩分（key=provider:track_id）；无数据回落乐观初值。"""
        return self._shrunk(self._bgm, str(bgm_track))

    @property
    def has_data(self) -> bool:
        if not self._loaded:
            self.load()
        return bool(self._mat or self._sp or self._pb)

    @property
    def has_playbook_data(self) -> bool:
        if not self._loaded:
            self.load()
        return bool(self._pb)


def record_performance(
    *,
    render: str,
    product: str,
    selling_point: str,
    material_ids: list[str],
    completion: float = 0.0,
    engagement: float = 0.0,
    views: float = 0.0,
    gmv: float = 0.0,
    ctr: float = 0.0,
    cvr: float = 0.0,
    playbook: str = "",
    beat_order: list[str] | None = None,
    angle: str = "",
    emotion: str = "",
    bgm_track: str = "",
    path: Path | None = None,
) -> None:
    """追加一条成片表现（发布后回流调用）。指标为 0~1 归一值。

    Director 路径额外回流 playbook/beat_order/angle/emotion，供 DirectorEngine 学习
    「什么结构/情绪更成交」。旧调用不传这些字段时行为不变。
    """
    p = Path(path or PERF_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "render": render, "product": product, "selling_point": selling_point,
        "material_ids": list(material_ids or []),
        "completion": completion, "engagement": engagement, "views": views, "gmv": gmv,
        "ctr": ctr, "cvr": cvr,
        "playbook": playbook, "beat_order": list(beat_order or []),
        "angle": angle, "emotion": emotion, "bgm_track": bgm_track,
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    get_performance_store.cache_clear()  # 下次选材重新加载


@lru_cache(maxsize=1)
def get_performance_store() -> PerformanceStore:
    s = get_settings()
    return PerformanceStore(
        path=PERF_PATH,
        weights={
            "gmv": s.perf_weight_gmv,
            "cvr": s.perf_weight_cvr,
            "ctr": s.perf_weight_ctr,
            "views": s.perf_weight_views,
            "completion": s.perf_weight_completion,
        },
        shrink_k=s.scoring_shrink_k,
        opt_init=s.scoring_optimistic_init,
    )
