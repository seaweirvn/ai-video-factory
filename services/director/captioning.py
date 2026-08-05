"""成片级字幕生成（Director 路径 · 镜头选完之后运行）。

P0 强接地：逐 beat 把 **Brief（角度/核心卖点/情绪/风格）+ 该 beat 每条已选 clip 的
shooting_content + material_type + 主/辅标签** 拼成「本段真实画面」上下文，喂给
CaptionGenerator.generate_stage_script，并在上下文里强约束「只讲这条画面在演的」，
保证文案与画面严格一致。

产出 {beat_name: [captions...]}，交给 compile_brief_to_storyboard 直接填入 Storyboard。
字幕文本同时用于配音，读法本地化（S2→Ét Hai）交给 Speech Formatter，保证「看到=听到」。
"""

from __future__ import annotations

import re
from collections import defaultdict

from loguru import logger

from app.config import get_settings
from core.models import Material
from services.caption import get_caption_generator
from services.director.models import Brief


def _brand_regex(brand: str, product: str) -> re.Pattern | None:
    """匹配「品牌 (+可选型号)」：如 SEAWEIR / SEAWEIR S2（大小写不敏感）。缺品牌名返回 None。"""
    brand = (brand or "").strip()
    if not brand:
        return None
    model = (product or "").strip()
    tail = rf"(?:\s*{re.escape(model)})?" if model else ""
    return re.compile(re.escape(brand) + tail, re.IGNORECASE)


def _tidy_after_strip(s: str) -> str:
    """抹掉品牌后清理残留：占位符、行首标点/空格、以及中间留下的重复逗号。"""
    s = s.replace("\x00", "")
    s = re.sub(r"^[\s，,、。!！?？:：\-—~～]+", "", s)          # 行首悬挂标点
    s = re.sub(r"[，,、]\s*(?=[，,、])", "", s)                 # 连续逗号去重
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def _limit_brand_mentions(
    out: dict[str, list[str]], order: list[str], brand: str, product: str, max_mentions: int
) -> dict[str, list[str]]:
    """把整条视频里「品牌(+型号)」出现次数限制到 max_mentions（默认 2），像人说话而非硬广。

    保留策略：留下**第一次**（通常是 Hook 开场）和**最后一次**（通常是 CTA），
    抹掉中间多余的品牌词；抹掉后句子仍通顺（清理悬挂标点）。抹空的句子回退原文（极少）。
    max_mentions<=0 或没有品牌名时不处理。
    """
    if max_mentions <= 0:
        return out
    brand_re = _brand_regex(brand, product)
    if brand_re is None:
        return out

    total = sum(len(brand_re.findall(ln)) for beat in order for ln in out.get(beat, []))
    if total <= max_mentions:
        return out

    # 要保留的品牌出现序号：前 (max-1) 个 + 最后 1 个（max=2 -> 第0个与最后一个）
    keep = set(range(max(0, max_mentions - 1))) | {total - 1}
    idx = 0
    removed = 0
    for beat in order:
        new_lines: list[str] = []
        for ln in out.get(beat, []):
            def _repl(m: re.Match) -> str:
                nonlocal idx, removed
                cur = idx
                idx += 1
                if cur in keep:
                    return m.group(0)
                removed += 1
                return "\x00"
            stripped = _tidy_after_strip(brand_re.sub(_repl, ln))
            new_lines.append(stripped if len(stripped) >= 2 else ln)
        out[beat] = new_lines
    logger.info(
        "品牌出现次数收敛 - brand={} product={} {}次 -> 保留{}次(抹掉{})",
        brand, product, total, min(total, max_mentions), removed,
    )
    return out


def _shot_context(brief: Brief, beat, clips, lookup: dict[str, Material]) -> str:
    lines: list[str] = []
    if brief.angle:
        lines.append(f"Video angle: {brief.angle}")
    if brief.emotion:
        lines.append(f"Target emotion to build: {brief.emotion}")
    lines.append(
        "STRICT: describe ONLY what is literally shown in the CURRENT shot(s) listed below. "
        "Never mention anything not visible on screen; do not invent features."
    )
    lines.append(f"This beat's goal: {beat.goal or beat.name}")
    for c in clips:
        m = lookup.get(c.record_id)
        if not m:
            continue
        on_screen = (m.shooting_content or m.material_type or "").strip()
        desc = f"- On screen: {on_screen or '(product footage)'}"
        if m.material_type:
            desc += f" [type: {m.material_type}]"
        tags = ([m.main_tag] if m.main_tag else []) + list(m.aux_tags or [])
        tags = [t for t in tags if t]
        if tags:
            desc += " | tags: " + ", ".join(tags[:6])
        lines.append(desc)
    return "\n".join(lines)


def generate_final_captions(
    brief: Brief, plan, lookup: dict[str, Material]
) -> dict[str, list[str]]:
    """逐 beat 生成最终字幕。返回 {beat_name: [captions...]}。

    字幕文本即口播文本：语音读同一句话，品牌/型号/单位的读音由 Speech Formatter 本地化
    （字幕显示 S2、语音读 Ét Hai），保证「看到的」和「听到的」是同一句话。
    任何单段失败都安全回落空列表，绝不打断生产。
    """
    gen = get_caption_generator()

    clips_by_beat: dict[str, list] = defaultdict(list)
    for c in plan.clips:
        clips_by_beat[c.beat or ""].append(c)

    out: dict[str, list[str]] = {}
    said: list[str] = []  # 全片已生成的字幕，逐段累加，喂给下一段做去重（避免同词反复）
    for beat in brief.beats:
        clips = clips_by_beat.get(beat.name, [])
        context = _shot_context(brief, beat, clips, lookup)
        caption_style = "; ".join(
            x for x in [
                beat.tone,
                brief.tone,
                f"emotion={brief.emotion}" if brief.emotion else "",
                brief.caption_style,
            ] if x
        )
        try:
            res = gen.generate_stage_script(
                product=brief.product,
                language=brief.market,
                main_selling_point=beat.selling_point or brief.core_selling_point,
                stage=beat.intent_type,
                slot_sec=beat.slot_sec,
                country=brief.country,
                target=brief.goal,
                variables={"product_name": brief.product},
                caption_style=caption_style,
                context=context,
                avoid_lines=list(said),
            )
            lines = list(res.get("captions") or [])
            out[beat.name] = lines
            said.extend(lines)
        except Exception as exc:  # noqa: BLE001 - 单段字幕失败不阻塞
            logger.warning("Brief 字幕生成失败(已吞) - beat={} err={}", beat.name, exc)
            out[beat.name] = []

    # 品牌控频：整条视频「品牌(+型号)」最多出现 N 次（默认 2，保留首个 Hook 与结尾 CTA），
    # 避免每句都念 SEAWEIR S2 像硬广。字幕即口播，抹掉后配音也同步减少。
    s = get_settings()
    out = _limit_brand_mentions(
        out, [b.name for b in brief.beats],
        brand=getattr(s, "brand_name", "SEAWEIR"),
        product=brief.product,
        max_mentions=int(getattr(s, "brand_max_mentions", 2) or 0),
    )

    total = sum(len(v or []) for v in out.values())
    logger.info("Brief 字幕生成完成 - product={} {}段 {}句", brief.product, len(out), total)
    return out
