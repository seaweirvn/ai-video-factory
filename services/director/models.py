"""Director Engine 的数据结构：Content Brief（销售方向）+ Beat（结构段）。

设计要点：
- Brief 只定义「这条视频怎么卖」——角度/核心卖点/情绪/表达方式/所需镜头/结构/文案风格，
  **不含任何最终字幕**。最终字幕由 CaptionService 在「镜头选完之后」结合 Brief + 已选镜头生成。
- 结构不写死：beats 从 config/playbooks.yaml 装配，可无限扩展（hook_proof_cta /
  question_demo_proof_cta / problem_solution_demo_cta ...）。
- 镜头节奏：默认每个 beat 单镜头连续铺满时间槽（max_clips=1），不做多片快切。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.enums import MaterialRole


@dataclass
class Beat:
    """一个 Story Beat（视频的一拍）。名字/顺序/数量都不写死，由 Director(LLM) 自由决定。

    描述「导演意图」：目的、想表达什么、需要什么镜头、建议时长、想建立的情绪。
    不含最终字幕——字幕在选镜头之后由 CaptionService 生成。
    """

    name: str
    purpose: str = ""                # 目的（吸引停留 / 建立信任 / 推动购买 ...）
    express: str = ""                # 想表达什么（这一拍要讲清的一件事）
    emotion: str = ""                # 想建立的情绪（curiosity / trust / desire / urgency ...）
    roles: list[MaterialRole] = field(default_factory=list)  # 可用哪些角色的素材
    intent_type: str = ""            # 字幕/文案 prompt 的 stage/intent（映射到 prompt_library）
    selling_point: str = ""          # 该段主打卖点（默认继承 Brief.core_selling_point）
    tone: str = ""                   # 语调：excited/friendly/confident/urgent/curious...
    goal: str = ""                   # = purpose 的英文简述（下游 prompt 用）
    shot_priority: list[str] = field(default_factory=list)  # 需要的镜头类型（枚举，越靠前越优先）
    weight: float = 1.0              # 兜底时长权重（无建议时长时用）
    min_clips: int = 1
    max_clips: int = 1               # 默认单镜头连续铺满（不快切）
    # 建议时长区间（导演给的软建议；最终由口播驱动并在 [min_sec, max_sec] 内 clamp）
    min_sec: float = 0.0
    max_sec: float = 0.0
    # 引擎回填：
    slot_sec: float = 0.0            # 文案规划目标时长（决定该段说多少）；最终渲染时长可浮动
    time_range: str = ""             # "0-3s"（由累计 slot 得出，仅供展示）

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "express": self.express,
            "emotion": self.emotion,
            "roles": [r.value for r in self.roles],
            "intent_type": self.intent_type,
            "selling_point": self.selling_point,
            "tone": self.tone,
            "goal": self.goal,
            "shot_priority": list(self.shot_priority),
            "weight": self.weight,
            "min_clips": self.min_clips,
            "max_clips": self.max_clips,
            "min_sec": self.min_sec,
            "max_sec": self.max_sec,
            "slot_sec": self.slot_sec,
            "time_range": self.time_range,
        }


@dataclass
class Brief:
    """Content Brief：这条视频的销售方向与组织方式。不含最终字幕。"""

    product: str
    market: str                      # = content_language（vi/th/ms/id...）
    country: str = ""
    goal: str = "conversion"         # 优化目标
    # —— 销售策略 ——
    angle: str = ""                  # 一句话主张：这条视频到底在讲什么
    core_selling_point: str = ""     # 核心卖点（selling_points.yaml 的 key）
    supporting_points: list[str] = field(default_factory=list)
    emotion: str = ""                # 要建立的用户情绪：desire/trust/curiosity/urgency...
    tone: str = ""                   # 整体语气
    caption_style: str = ""          # 文案风格描述
    audio_mood: str = ""             # BGM 情绪槽：energetic/chill/suspense/upbeat...
    # —— 结构 ——
    playbook: str = ""               # 采用的结构名
    duration: int = 0                # 目标总时长（秒）
    variant: str = ""                # full / compact（沿用兼容）
    beats: list[Beat] = field(default_factory=list)
    # —— 归因/审校 ——
    rationale: str = ""              # 为什么这么决策（人读/复盘）
    scores: dict = field(default_factory=dict)  # 各候选 playbook/卖点的决策分
    adjustments: list[str] = field(default_factory=list)  # Selection 阶段对 beats 的自动调整记录
    needs_review: bool = False
    source: str = "heuristic"        # story beats 来源：llm / heuristic

    def to_dict(self) -> dict:
        return {
            "product": self.product,
            "market": self.market,
            "country": self.country,
            "goal": self.goal,
            "angle": self.angle,
            "core_selling_point": self.core_selling_point,
            "supporting_points": list(self.supporting_points),
            "emotion": self.emotion,
            "tone": self.tone,
            "caption_style": self.caption_style,
            "audio_mood": self.audio_mood,
            "playbook": self.playbook,
            "duration": self.duration,
            "variant": self.variant,
            "beats": [b.to_dict() for b in self.beats],
            "rationale": self.rationale,
            "scores": self.scores,
            "adjustments": list(self.adjustments),
            "needs_review": self.needs_review,
            "source": self.source,
        }


@dataclass
class MaterialInventory:
    """产品能力盘点：Director 决策的输入（不含被选中的具体 clip，只有「能力」摘要）。

    让 Director 做出「可落地」的策略决策——先决定讲什么，但不会决定出素材撑不起来的角度。
    """

    product: str
    total_available_sec: float = 0.0
    role_counts: dict[str, int] = field(default_factory=dict)          # role.value -> 条数
    selling_point_weights: dict[str, float] = field(default_factory=dict)  # 卖点 -> 证据权重
    ranked_selling_points: list[str] = field(default_factory=list)     # 证据(+表现)降序
    shot_enum_counts: dict[str, int] = field(default_factory=dict)     # 镜头枚举 -> 覆盖条数
    material_count: int = 0

    def has_role(self, role: MaterialRole) -> bool:
        return self.role_counts.get(role.value, 0) > 0

    def has_shots(self, enums: list[str]) -> bool:
        return any(self.shot_enum_counts.get(e, 0) > 0 for e in enums)

    def to_dict(self) -> dict:
        return {
            "product": self.product,
            "total_available_sec": round(self.total_available_sec, 2),
            "role_counts": dict(self.role_counts),
            "selling_point_weights": {k: round(v, 3) for k, v in self.selling_point_weights.items()},
            "ranked_selling_points": list(self.ranked_selling_points),
            "shot_enum_counts": dict(self.shot_enum_counts),
            "material_count": self.material_count,
        }
