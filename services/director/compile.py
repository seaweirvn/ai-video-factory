"""compile_brief_to_storyboard：把 Brief + RenderPlan + 已生成字幕「纯装配」成现有 Storyboard。

纯装配：无 LLM、无任何决策——决策已在 DirectorEngine(结构/卖点) 与 SelectionService(镜头) 与
CaptionService(字幕) 做完。这里只把它们拼进现有 Storyboard(StageSpec[])，让下游
voiceover / edit / compose 一行不改地执行（= 执行 Timeline）。
"""

from __future__ import annotations

from collections import defaultdict

from services.director.models import Brief
from services.storyboard.models import Storyboard, StageSpec, SubtitleIntent


def compile_brief_to_storyboard(
    brief: Brief, plan, captions: dict | None = None
) -> Storyboard:
    """captions: {beat_name: [字幕文本...]}。字幕即口播文本，读音本地化由 Speech Formatter 处理。"""
    captions = captions or {}

    clips_by_beat: dict[str, list[str]] = defaultdict(list)
    for c in plan.clips:
        clips_by_beat[c.beat or ""].append(c.record_id)

    specs: list[StageSpec] = []
    for beat in brief.beats:
        intent = SubtitleIntent(
            type=beat.intent_type,
            selling_point=beat.selling_point or brief.core_selling_point,
            tone=beat.tone,
        )
        spec = StageSpec(
            stage=beat.name,
            time_range=beat.time_range,
            goal=beat.goal,
            shot_type=", ".join(beat.shot_priority[:3]),
            subtitle_intent=intent,
            slot_sec=beat.slot_sec,
            min_sec=beat.min_sec,
            max_sec=beat.max_sec,
            clip_record_ids=list(clips_by_beat.get(beat.name, [])),
        )
        data = captions.get(beat.name)
        if isinstance(data, dict):  # 兼容旧的两版结构，取显示版即可
            spec.resolved_subtitles = list(data.get("captions") or [])
        else:
            spec.resolved_subtitles = list(data or [])
        spec.resolved_tts = []  # 不再单独生成口播文本：语音直接读字幕（读音由 Speech Formatter 本地化）
        specs.append(spec)

    return Storyboard(
        product=brief.product,
        main_selling_point=brief.core_selling_point,
        market=brief.market,
        target=brief.goal,
        duration=int(brief.duration or 0),
        variant=brief.variant,
        structure=specs,
        needs_localization_review=brief.needs_review,
    )
