"""Storyboard 数据结构（Conversion Director 的产出）。

固定四段 hook/value/proof/cta。字幕字段只存「语义意图」(SubtitleIntent)，
不存具体文案 —— 具体文案由 SubtitleResolver 在渲染前按 market 解析出来。
每个 stage 可绑定多条 RenderClip（clip_record_ids），解析时按 clip 顺序分配候选文案。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubtitleIntent:
    """字幕语义意图（不含具体文案）。"""

    type: str            # 四个固定值之一：hook_strong_attraction / value_benefit / proof_claim / cta_purchase
    selling_point: str   # 英文卖点枚举（selling_points.yaml 的 key）
    tone: str = ""       # 语调：excited / friendly / confident / urgent

    def to_dict(self) -> dict:
        return {"type": self.type, "selling_point": self.selling_point, "tone": self.tone}


@dataclass
class StageSpec:
    """一个结构段：hook / value / proof / cta。"""

    stage: str                                   # hook / value / proof / cta / question / demo ...
    time_range: str                              # "0-3s"
    goal: str                                    # "stop scrolling"
    shot_type: str                               # 语义镜头描述（来自 shot_priority.yaml）
    subtitle_intent: SubtitleIntent
    slot_sec: float = 0.0                         # 文案规划目标时长（决定说多少）
    clip_record_ids: list[str] = field(default_factory=list)  # 绑定到该段的成片片段（可 0..n 条）
    # 自由时长边界（Director 建议）：最终渲染时长由口播驱动并 clamp 到 [min_sec, max_sec]（0=不限）
    min_sec: float = 0.0
    max_sec: float = 0.0
    # 解析阶段回填：按 clip 顺序分配的最终文案（与 clip_record_ids 等长；无 clip 时为单句预览）
    resolved_subtitles: list[str] = field(default_factory=list)
    # 口播文本（本地读法版本，与 resolved_subtitles 一一对应）；空则口播回落用 resolved_subtitles。
    # 字幕/显示永远用 resolved_subtitles；TTS 用 resolved_tts（再经 Speech Formatter）。
    resolved_tts: list[str] = field(default_factory=list)

    def to_dict(self, *, include_resolved: bool = False) -> dict:
        d: dict = {
            "stage": self.stage,
            "time_range": self.time_range,
            "goal": self.goal,
            "shot_type": self.shot_type,
            "slot_sec": self.slot_sec,
            "min_sec": self.min_sec,
            "max_sec": self.max_sec,
            "subtitle_intent": self.subtitle_intent.to_dict(),
            "clip_record_ids": list(self.clip_record_ids),
        }
        if include_resolved:
            d["resolved_subtitles"] = list(self.resolved_subtitles)
            d["resolved_tts"] = list(self.resolved_tts)
        return d


@dataclass
class Storyboard:
    """一条成片的成交结构脚本。剪辑器只执行本结构，不自行决定镜头顺序。"""

    product: str
    main_selling_point: str
    market: str                  # = content_language（vi/th/ms/id...），见 config 文档
    target: str                  # 优化目标（conversion）
    duration: int                # 目标总时长（秒）
    variant: str                 # full / compact
    structure: list[StageSpec] = field(default_factory=list)
    needs_localization_review: bool = False  # 命中不到 market 配置、回落默认市场时置 True

    def to_dict(self, *, include_resolved: bool = False) -> dict:
        return {
            "product": self.product,
            "main_selling_point": self.main_selling_point,
            "market": self.market,
            "target": self.target,
            "duration": self.duration,
            "variant": self.variant,
            "needs_localization_review": self.needs_localization_review,
            "structure": [s.to_dict(include_resolved=include_resolved) for s in self.structure],
        }
