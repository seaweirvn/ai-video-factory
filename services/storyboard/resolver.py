"""Subtitle Resolver：把「字幕语义意图」解析成具体文案。

解析优先级（见 resolve_subtitle docstring）：
  1. locales/{market}/subtitles.yaml 的 intent_type -> selling_point
  2. 回落该 intent_type 下的 default 分组
  3. default 仍为空 -> 调 AI 文案生成
  4. AI 结果异步回写到对应 yaml 的 default（模板库自增长）
  5. market 配置文件不存在 -> warning + 回落默认市场(vi) + 标记 needs_localization_review，不阻断
  6. 所有异常都捕获记录，绝不让整条视频渲染失败
"""

from __future__ import annotations

from typing import Callable

from loguru import logger

from services.storyboard import config as sbcfg
from services.storyboard.models import Storyboard

# AI 生成器签名：(market, intent_type, selling_point, variables) -> str（返回一句字幕）
AiGenerator = Callable[[str, str, str, dict], str]


def _apply_vars(text: str, variables: dict | None) -> str:
    if not text or not variables:
        return text or ""
    out = text
    for key, val in variables.items():
        out = out.replace("{" + str(key) + "}", str(val))
    return out


def _candidates(market: str, intent_type: str, selling_point: str) -> tuple[list[str], bool, str]:
    """返回 (候选文案列表, needs_review, 生效market)。不触发 AI，不做变量替换。"""
    review = False
    eff_market = market
    if not sbcfg.locale_exists(market):
        logger.warning(
            "市场[{}]字幕配置缺失，回落默认市场[{}]并标记待本地化审校",
            market, sbcfg.DEFAULT_MARKET,
        )
        eff_market = sbcfg.DEFAULT_MARKET
        review = True

    data = sbcfg.load_locale_subtitles(eff_market)
    node = data.get(intent_type) or {}
    if not isinstance(node, dict):
        node = {}

    specific = node.get(selling_point)
    if isinstance(specific, list) and specific:
        return [str(x) for x in specific if x], review, eff_market

    default_bucket = node.get("default")
    if isinstance(default_bucket, list) and default_bucket:
        return [str(x) for x in default_bucket if x], review, eff_market

    return [], review, eff_market


def _default_ai_generator(market: str, intent_type: str, selling_point: str, variables: dict) -> str:
    """默认 AI 兜底：优先走多语言 Prompt Library(CaptionGenerator)，按 语言/阶段 读 prompt 生成；
    为空再回落原 ContentProvider。延迟导入，缺 key 时安全返回空，绝不抛出。"""
    product = str((variables or {}).get("product_name") or selling_point)
    # 1) Prompt Library 驱动（不写死文案，按 market+stage 读 prompt_library/*.yaml）
    try:
        from services.caption import get_caption_generator

        res = get_caption_generator().generate(
            product=product,
            language=market or sbcfg.DEFAULT_MARKET,
            main_selling_point=selling_point,
            stage=intent_type,  # 内部 stage_for_intent 会把 intent_type 归一到 hook/value/proof/cta
            variables=variables,
        )
        cap = (res.get("caption") or "").strip()
        if cap:
            return cap
    except Exception as exc:  # noqa: BLE001 - Prompt Library 失败不阻塞，继续回落
        logger.warning("Prompt Library 字幕生成失败，回落原 provider - {}", exc)

    # 2) 回落：原 ContentProvider
    try:
        from adapters.ai_providers import get_content_provider

        provider = get_content_provider()
        res2 = provider.generate_caption(
            product, [selling_point], market or sbcfg.DEFAULT_MARKET,
            video_type=intent_type,
        )
        text = (res2.get("title") or res2.get("caption") or "").strip()
        return text.splitlines()[0][:60] if text else ""
    except Exception as exc:  # noqa: BLE001 - AI 兜底失败不阻塞
        logger.warning("AI 字幕兜底失败 - {}", exc)
        return ""


def resolve_subtitle(
    market: str,
    intent_type: str,
    selling_point: str,
    variables: dict | None = None,
    *,
    ai_generator: AiGenerator | None = None,
) -> str:
    """解析单句字幕文案（见模块 docstring 的优先级）。任何异常都返回安全值，不抛出。"""
    try:
        cands, _review, eff_market = _candidates(market, intent_type, selling_point)
        if cands:
            return _apply_vars(cands[0], variables)

        # 模板池为空 -> AI 生成 -> 异步回写 default
        gen = ai_generator or _default_ai_generator
        text = gen(eff_market, intent_type, selling_point, variables or {})
        if text:
            sbcfg.append_locale_default(eff_market, intent_type, text)
            return _apply_vars(text, variables)

        logger.warning(
            "字幕无模板且 AI 兜底为空 - market={} intent={} sp={}",
            market, intent_type, selling_point,
        )
        return ""
    except Exception as exc:  # noqa: BLE001 - 绝不因字幕炸掉渲染
        logger.warning("resolve_subtitle 异常(已吞) - {}", exc)
        return ""


def resolve_intent_list(
    market: str,
    intent_type: str,
    selling_point: str,
    n: int,
    variables: dict | None = None,
    *,
    ai_generator: AiGenerator | None = None,
) -> tuple[list[str], bool]:
    """为 n 条 clip 解析 n 句文案：按候选顺序分配，不够则相邻 clip 共用同一句。

    返回 (文案列表[长度=max(n,1)], needs_review)。
    """
    n = max(int(n), 1)
    try:
        cands, review, eff_market = _candidates(market, intent_type, selling_point)
        if not cands:
            gen = ai_generator or _default_ai_generator
            text = gen(eff_market, intent_type, selling_point, variables or {})
            if text:
                sbcfg.append_locale_default(eff_market, intent_type, text)
                cands = [text]
        if not cands:
            return [""] * n, review
        out = [_apply_vars(cands[min(i, len(cands) - 1)], variables) for i in range(n)]
        return out, review
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_intent_list 异常(已吞) - {}", exc)
        return [""] * n, False


def resolve_storyboard(
    storyboard: Storyboard,
    variables: dict | None = None,
    *,
    ai_generator: AiGenerator | None = None,
) -> Storyboard:
    """就地解析整个 storyboard：每段按绑定 clip 数分配文案，回填 resolved_subtitles。

    变量默认注入 {product_name}=storyboard.product；调用方可用 variables 覆盖/追加。
    """
    base_vars = {"product_name": storyboard.product}
    if variables:
        base_vars.update(variables)

    any_review = False
    for stage in storyboard.structure:
        intent = stage.subtitle_intent
        n = max(len(stage.clip_record_ids), 1)
        lines, review = resolve_intent_list(
            storyboard.market, intent.type, intent.selling_point, n,
            base_vars, ai_generator=ai_generator,
        )
        stage.resolved_subtitles = lines
        any_review = any_review or review

    storyboard.needs_localization_review = any_review
    return storyboard


def resolve_storyboard_script(
    storyboard: Storyboard,
    variables: dict | None = None,
    *,
    context: str = "",
) -> Storyboard:
    """成交结构专用：每段用多语言 Prompt Library 生成「填满时间槽的多句脚本」，回填 resolved_subtitles。

    与 resolve_storyboard（一句/段的模板解析）不同：本函数按 stage 时间槽产出多句连贯口播，
    让字幕连续铺满整段。生成走 CaptionGenerator（prompt_library/*.yaml），失败对该段安全回落空列表，
    绝不打断渲染。
    """
    base_vars = {"product_name": storyboard.product}
    if variables:
        base_vars.update(variables)

    try:
        from services.caption import get_caption_generator

        gen = get_caption_generator()
    except Exception as exc:  # noqa: BLE001 - 拿不到生成器则回落原逐句解析
        logger.warning("CaptionGenerator 不可用，回落 resolve_storyboard - {}", exc)
        return resolve_storyboard(storyboard, variables=variables)

    for stage in storyboard.structure:
        intent = stage.subtitle_intent
        try:
            res = gen.generate_stage_script(
                product=storyboard.product,
                language=storyboard.market,
                main_selling_point=intent.selling_point,
                stage=intent.type,
                slot_sec=stage.slot_sec,
                target=storyboard.target,
                variables=base_vars,
                context=context,
            )
            stage.resolved_subtitles = list(res.get("captions") or [])
        except Exception as exc:  # noqa: BLE001 - 单段失败不阻塞
            logger.warning("段脚本解析失败(已吞) - stage={} err={}", stage.stage, exc)
            stage.resolved_subtitles = []

    return storyboard
