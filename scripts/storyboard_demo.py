"""Storyboard + Subtitle Resolver 最小可跑通 demo。

跑法：  .venv\\Scripts\\python.exe scripts\\storyboard_demo.py

演示：
  1) 生成一份 Storyboard JSON（含 subtitle_intent，不含具体文案）
  2) market=vi 解析后每个 stage 的最终字幕
  3) market=kh（不存在）验证回落 vi + needs_localization_review
不依赖网络/素材库：默认用合成 RenderPlan（每段可多 clip，演示"相邻 clip 共用同一句"）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.enums import MaterialRole  # noqa: E402
from core.models import RenderClip, RenderPlan  # noqa: E402
from services.storyboard import (  # noqa: E402
    get_conversion_director,
    resolve_storyboard,
    resolve_subtitle,
)


def _synthetic_plan() -> RenderPlan:
    """1 HOOK / 2 VALUE / 2 PROOF / 1 CTA —— 中间段多 clip，用于演示按 clip 分配文案。"""
    def clip(rid: str, role: MaterialRole, dur: float) -> RenderClip:
        return RenderClip(record_id=rid, material_id=rid, role_used=role, duration_sec=dur)

    return RenderPlan(
        product_model="S2",
        clips=[
            clip("rec_hook", MaterialRole.hook, 3.0),
            clip("rec_val1", MaterialRole.value, 3.5),
            clip("rec_val2", MaterialRole.value, 3.5),
            clip("rec_prf1", MaterialRole.proof, 4.0),
            clip("rec_prf2", MaterialRole.proof, 4.0),
            clip("rec_cta", MaterialRole.cta, 3.0),
        ],
        target_duration_sec=25.0,
    )


def _print(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> None:
    director = get_conversion_director()
    plan = _synthetic_plan()

    # —— 交付物 1：Storyboard JSON（subtitle_intent，不含文案）——
    sb = director.build(plan, market="vi", main_selling_point="smooth_retrieve")
    _print("[1] Storyboard JSON（含 subtitle_intent，不含具体文案）")
    print(json.dumps(sb.to_dict(include_resolved=False), ensure_ascii=False, indent=2))

    # —— 交付物 2：market=vi 解析结果 ——
    resolve_storyboard(sb)
    _print("[2] market=vi 解析后（每 stage 按绑定 clip 数分配文案）")
    for st in sb.structure:
        print(f"- {st.stage:5s} intent={st.subtitle_intent.type:22s} clips={len(st.clip_record_ids)}")
        for rid, line in zip(st.clip_record_ids, st.resolved_subtitles):
            print(f"    {rid:10s} -> {line}")
    print(f"needs_localization_review = {sb.needs_localization_review}")
    print("单句 API 示例 resolve_subtitle(vi, cta_purchase, smooth_retrieve):")
    print("   ", resolve_subtitle("vi", "cta_purchase", "smooth_retrieve", {"product_name": "Seaweir S2"}))

    # —— 交付物 3：market=kh（不存在）回落验证 ——
    sb_kh = director.build(plan, market="kh", main_selling_point="smooth_retrieve")
    resolve_storyboard(sb_kh)
    _print("[3] market=kh（不存在）→ 回落 vi + needs_localization_review")
    for st in sb_kh.structure:
        print(f"- {st.stage:5s} -> {st.resolved_subtitles}")
    print(f"needs_localization_review = {sb_kh.needs_localization_review}  (期望 True)")


if __name__ == "__main__":
    main()
