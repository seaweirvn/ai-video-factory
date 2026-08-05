"""生产编排：把「选材 →（可选）配音 → 剪辑 → 上传 → 文案回写」串成一个动作。

给 n8n 用：n8n 只需按天定时调 /produce，复杂链路都在这里。
每个产品可一次产 N 条；单条失败不影响其它条，错误汇总在返回里。
"""

from __future__ import annotations

import dataclasses as _dc
import json
import random
import time
from functools import lru_cache
from pathlib import Path

from loguru import logger

from adapters.feishu import make_feishu_client
from app.config import get_settings
from services.content import ContentService, get_content_service
from services.director import (
    compile_brief_to_storyboard,
    generate_final_captions,
    get_director_engine,
    select_bgm_detailed,
)
from services.edit import EditService, get_edit_service
from services.selection import SelectionService, get_selection_service
from services.storyboard import get_conversion_director, resolve_storyboard_script
from services.storyboard.models import Storyboard
from services.voiceover import VoiceoverService, get_voiceover_service


def _ts() -> str:
    """成片名用的时间戳（本地时区，精确到秒）。"""
    return time.strftime("%Y%m%d-%H%M%S")


def _storyboard_has_lines(sb: Storyboard) -> bool:
    """Storyboard 是否至少解析出一句可用字幕（判断能否走结构渲染）。"""
    return any(
        any((x or "").strip() for x in stage.resolved_subtitles) for stage in sb.structure
    )


def _product_context(ctx) -> str:
    """从内容上下文拼出「产品品类接地」文本，喂给字幕生成，杜绝跑偏到非本品类领域。"""
    if ctx is None:
        return ""
    parts: list[str] = []
    if getattr(ctx, "product_positioning", ""):
        parts.append(str(ctx.product_positioning))
    sps = [str(s).strip() for s in (getattr(ctx, "product_selling_points", []) or []) if str(s).strip()]
    if sps:
        parts.append("Product: " + "; ".join(sps[:6]))
    tags = ([getattr(ctx, "primary_tag", "")] if getattr(ctx, "primary_tag", "") else []) + list(
        getattr(ctx, "secondary_tags", []) or []
    )
    tags = [t for t in tags if t]
    if tags:
        parts.append("Footage tags: " + ", ".join(tags[:8]))
    if getattr(ctx, "shooting_content", ""):
        parts.append("On screen: " + str(ctx.shooting_content))
    return " | ".join(parts)


class ProduceService:
    def __init__(
        self,
        selection: SelectionService,
        edit: EditService,
        content: ContentService,
        voiceover: VoiceoverService,
        settings,
    ) -> None:
        self.selection = selection
        self.edit = edit
        self.content = content
        self.voiceover = voiceover
        self.s = settings

    def produce_batch(
        self,
        products: list[str] | None = None,
        count: int = 1,
        target_duration_sec: float | None = None,
        voiceover_enabled: bool | None = None,
        language: str | None = None,
        voice: str | None = None,
        upload: bool = True,
        generate_content: bool = True,
        progress=None,
    ) -> dict:
        """批量生产：products 为空则自动发现所有可组片的产品型号。"""
        products = products or self.selection.producible_products()
        if not products:
            raise RuntimeError("没有可组片的产品（缺 HOOK 或 CTA 素材）")

        batches: list[dict] = []
        total_produced = 0
        m = len(products)
        for i, product in enumerate(products):
            def prog(p: float, _i: int = i, _m: int = m) -> None:
                if progress:
                    progress(min(1.0, (_i + p) / _m))

            batch = self.produce(
                product_model=product,
                count=count,
                target_duration_sec=target_duration_sec,
                voiceover_enabled=voiceover_enabled,
                language=language,
                voice=voice,
                upload=upload,
                generate_content=generate_content,
                progress=prog,
            )
            total_produced += batch["produced"]
            batches.append(batch)

        if progress:
            progress(1.0)
        return {
            "products": products,
            "produced": total_produced,
            "batches": batches,
        }

    def produce(
        self,
        product_model: str,
        count: int = 1,
        target_duration_sec: float | None = None,
        voiceover_enabled: bool | None = None,
        language: str | None = None,
        voice: str | None = None,
        upload: bool = True,
        generate_content: bool = True,
        tags: list[str] | None = None,
        progress=None,
    ) -> dict:
        vo_enabled = (
            self.s.voiceover_enabled_default if voiceover_enabled is None else voiceover_enabled
        )
        if vo_enabled and not self.voiceover.available:
            raise RuntimeError("配音模式需要 OPENAI_API_KEY（TTS 未启用）")
        target = target_duration_sec or self.s.selection_target_duration_sec
        tags = tags or []

        results: list[dict] = []
        errors: list[str] = []
        n = max(count, 1)
        for idx in range(n):
            base = idx / n

            def prog(p: float, _base: float = base, _n: int = n) -> None:
                if progress:
                    progress(min(1.0, _base + p / _n))

            try:
                results.append(
                    self._produce_one(
                        product_model, idx, target, vo_enabled,
                        language, voice, tags, upload, generate_content, prog,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞整批
                logger.exception("生产失败 - product={} #{}", product_model, idx)
                errors.append(f"#{idx}: {exc}")

        if progress:
            progress(1.0)
        return {
            "product_model": product_model,
            "requested": count,
            "produced": len(results),
            "renders": results,
            "errors": errors,
        }

    def _produce_one(
        self,
        product_model: str,
        idx: int,
        target: float,
        vo_enabled: bool,
        language: str | None,
        voice: str | None,
        tags: list[str],
        upload: bool,
        generate_content: bool,
        prog,
    ) -> dict:
        # Director 路径（大脑先行）：先产 Brief -> 按 Brief 选镜头 -> 结合镜头写字幕 -> 装配 Timeline。
        # 需配合配音（时间槽渲染）；开关开启但未配音时回落原链路。
        if self.s.director_enabled:
            if vo_enabled:
                return self._produce_one_director(
                    product_model, idx, target, language, voice, tags,
                    upload, generate_content, prog,
                )
            logger.warning("director_enabled 但未开配音，回落原链路 - {}", product_model)

        # 1) 先选材，确定画面（口播/文案据此接地：只讲画面已体现的内容）
        prog(0.03)
        plan = self.selection.plan(
            product_model=product_model, count=1, target_duration_sec=target,
        )[0]

        # 2) 组装上下文（素材类型/拍摄内容/主辅标签 + 产品中心），供文案接地/卖点判定
        ctx = None
        if generate_content or vo_enabled:
            try:
                ctx = self.content.build_context(
                    plan,
                    language=language,
                    target_sec=plan.total_duration_sec,
                    emotion=self.voiceover.emotion,
                )
            except Exception:  # noqa: BLE001 - 生成失败不阻塞成片，后续走兜底
                logger.exception("内容上下文组装失败 - product={} #{}", product_model, idx)
                ctx = None

        # 2.5) 成交结构（Storyboard）：开关开启且配音时，用它按时间槽驱动画面/口播/字幕
        storyboard = None
        if self.s.storyboard_enabled and vo_enabled:
            try:
                sp_tags: list[str] = []
                if ctx is not None:
                    sp_tags = ([ctx.primary_tag] if ctx.primary_tag else []) + list(ctx.secondary_tags)
                market = language or self.s.content_language  # market = content_language 别名
                storyboard = get_conversion_director().build(
                    plan, market=market,
                    main_selling_point=plan.selling_point or None,  # Layer 2：对齐选材决定的卖点
                    selling_point_tags=sp_tags or None,
                )
                # 每段生成「填满时间槽的多句脚本」（统一走多语言 Prompt Library），字幕连续铺满。
                # 传产品上下文接地，避免文案跑偏到非本品类领域。
                resolve_storyboard_script(
                    storyboard,
                    variables={"product_name": product_model},
                    context=_product_context(ctx),
                )
                if not _storyboard_has_lines(storyboard):
                    logger.warning("Storyboard 解析出空口播，回落原文案链路 - {}", product_model)
                    storyboard = None
            except Exception:  # noqa: BLE001 - Storyboard 失败不阻塞，回落原链路
                logger.exception("Storyboard 生成失败，回落原文案链路 - {}", product_model)
                storyboard = None

        # 3) 内容包（标题/文案/话题标签）；Storyboard 已驱动口播时不再让 pack 生成 segments
        pack = None
        want_segments = vo_enabled and storyboard is None
        if (generate_content or vo_enabled) and ctx is not None:
            try:
                pack = self.content.generate_pack(ctx, want_segments=want_segments)
            except Exception:  # noqa: BLE001 - 生成失败不阻塞成片，后续走兜底
                logger.exception("内容包生成失败 - product={} #{}", product_model, idx)
                pack = None

        # 4) 渲染
        if storyboard is not None:
            # 时间槽对齐路径：每段画面铺满其时间槽，字幕/配音按绝对时轴对齐
            sb_voice = self.voiceover.build_storyboard(
                storyboard, name=f"{product_model or 'NA'}-{idx}", language=language, voice=voice,
            )
            logger.info(
                "Storyboard 时间槽渲染 - product={} market={} sp={} 目标{}s",
                product_model, storyboard.market, storyboard.main_selling_point, storyboard.duration,
            )
            result = self.edit.render_storyboard(
                plan, storyboard, sb_voice, upload=upload, progress=prog,
                kept_volume=self.s.voiceover_kept_original_volume,
            )
        elif vo_enabled:
            segments = pack.segments if (pack and pack.segments) else None
            asset = self.voiceover.build(
                product_model=product_model,
                tags=tags,
                target_sec=plan.total_duration_sec,
                language=language,
                voice=voice,
                name=f"{product_model or 'NA'}-{idx}",
                segments=segments,
            )
            # 画面必须覆盖配音：配音不超画面则复用同一 plan（口播贴合画面）；
            # 仅当配音更长时才按配音时长重选，保证画面不断。
            if asset.total_duration > plan.total_duration_sec:
                plan = self.selection.plan(
                    product_model=product_model,
                    count=1,
                    target_duration_sec=asset.total_duration + self.s.voiceover_tail_margin_sec,
                )[0]
            result = self.edit.render(
                plan, upload=upload, voiceover=asset,
                kept_volume=self.s.voiceover_kept_original_volume, progress=prog,
            )
        else:
            result = self.edit.render(plan, upload=upload, progress=prog)

        summary = {
            "name": result.name,
            "product_model": result.product_model,
            "duration_sec": result.duration_sec,
            "onedrive_link": result.onedrive_link,
            "feishu_record_id": result.feishu_record_id,
            "voiceover": result.voiceover,
        }
        # Storyboard 归因：落一份 JSON 到 data/renders/<name>.storyboard.json，并回填 summary
        if storyboard is not None:
            summary["storyboard"] = {
                "main_selling_point": storyboard.main_selling_point,
                "market": storyboard.market,
                "variant": storyboard.variant,
                "needs_localization_review": storyboard.needs_localization_review,
            }
            self._persist_storyboard(result.name, storyboard)

        # 5) 文案写回：优先复用上面生成的 pack（避免二次 GPT 调用）；缺失则按映射兜底
        if generate_content and result.feishu_record_id:
            try:
                if pack is not None:
                    self.content.write_pack(result.feishu_record_id, pack)
                    summary["title"] = pack.title
                    summary["caption"] = pack.caption
                    summary["tags"] = pack.hashtags
                else:
                    content = self.content.generate_and_write(
                        result.name, result.feishu_record_id, language
                    )
                    summary["title"] = content.get("title", "")
                    summary["caption"] = content.get("caption", "")
                    summary["tags"] = content.get("tags", [])
            except Exception:  # noqa: BLE001 - 文案失败不影响成片
                logger.exception("文案生成/写回失败（成片已生成）- {}", result.name)
        return summary

    def _produce_one_director(
        self,
        product_model: str,
        idx: int,
        target: float,
        language: str | None,
        voice: str | None,
        tags: list[str],
        upload: bool,
        generate_content: bool,
        prog,
        *,
        market_override: str | None = None,
        country_override: str | None = None,
        voice_override: str | None = None,
        feishu_client=None,
        table_id: str = "",
        name_override: str | None = None,
        produce_cn: bool = True,
    ) -> dict:
        """Director 路径：Brief -> 选镜头 -> 结合镜头写字幕 -> compile -> 配音 -> 渲染。

        默认按越南市场产 + 写越南成片表；传 *_override / feishu_client / table_id 时可复用
        同一条链路为其它市场（如中文版）做「独立选材+独立剪辑」并写对应成片表。
        produce_cn=False 时不再追加中文版（防止中文独立产出时递归）。
        """
        market = market_override or language or self.s.content_language
        vo_lang = market_override or language  # 覆盖市场时字幕/配音语言随之切换
        vo_voice = voice_override or voice

        # 1) 大脑：产出 Content Brief（销售方向，不含最终字幕）
        prog(0.03)
        brief = get_director_engine().plan(product_model, market=market, goal="conversion")
        if country_override:
            brief = _dc.replace(brief, country=country_override)

        # 2) 按 Brief 选镜头（每 beat 默认单镜头，不快切）
        plan = self.selection.plan_for_brief(brief)
        prog(0.2)

        # 3) 镜头选完后再写最终字幕：结合 Brief + 每条已选 clip 的画面语义，保证文案与画面一致
        lookup = {m.record_id: m for m in self.selection.repository.load_all()}
        captions = generate_final_captions(brief, plan, lookup)

        # 4) 纯装配成 Storyboard（Timeline）
        storyboard = compile_brief_to_storyboard(brief, plan, captions)
        if not _storyboard_has_lines(storyboard):
            # 安全网：字幕全空时用原逐段解析兜底，避免成片无口播/字幕
            logger.warning("Brief 字幕全空，启用逐段解析兜底 - {}", product_model)
            try:
                resolve_storyboard_script(
                    storyboard, variables={"product_name": product_model}
                )
            except Exception:  # noqa: BLE001
                logger.exception("兜底逐段解析失败 - {}", product_model)

        # 5) 配音（自由时长）：每段时长由口播驱动，clamp 到 beat[min,max] 与全片[15,30]
        from services.director.config import load_video_constraints

        vdur = (load_video_constraints().get("video_duration") or {})
        clip_durations = {c.beat: float(c.duration_sec or 0.0) for c in plan.clips if c.beat}
        sb_voice = self.voiceover.build_storyboard(
            storyboard, name=f"{product_model or 'NA'}-{idx}", language=vo_lang, voice=vo_voice,
            clip_durations=clip_durations,
            video_min_sec=float(vdur.get("min") or 0),
            video_max_sec=float(vdur.get("max") or 0),
        )
        prog(0.5)

        # 5.5) BGM（P1）：每条视频按概率（默认 50%）决定是否加背景音乐；
        # 加则按「情绪 + 国家」优先复用音乐库 GMV 高分曲（ε 概率在线拉新扩库），兜底本地/无曲。
        bgm = None
        bgm_track = None
        use_bgm = self.s.director_bgm_enabled and (
            random.random() < float(getattr(self.s, "director_bgm_probability", 0.5) or 0.0)
        )
        logger.info("BGM 掷骰 - product={} 加BGM={}（p={}）",
                    product_model, use_bgm, getattr(self.s, "director_bgm_probability", 0.5))
        if use_bgm:
            try:
                bgm, bgm_track = select_bgm_detailed(brief.audio_mood, brief.country or market)
            except Exception:  # noqa: BLE001 - BGM 选择失败不阻塞
                bgm, bgm_track = None, None

        # 6) 渲染（Renderer 只执行 Timeline）
        logger.info(
            "Director 渲染 - product={} playbook={} sp={} emotion={} {}s",
            product_model, brief.playbook, brief.core_selling_point, brief.emotion, storyboard.duration,
        )
        result = self.edit.render_storyboard(
            plan, storyboard, sb_voice, name=name_override, upload=upload, progress=prog,
            bgm=bgm, bgm_volume=self.s.director_bgm_volume,
            kept_volume=self.s.voiceover_kept_original_volume,
            feishu_client=feishu_client, table_id=table_id,
        )

        summary = {
            "name": result.name,
            "product_model": result.product_model,
            "duration_sec": result.duration_sec,
            "onedrive_link": result.onedrive_link,
            "feishu_record_id": result.feishu_record_id,
            "voiceover": result.voiceover,
            "brief": {
                "source": brief.source,
                "playbook": brief.playbook,
                "core_selling_point": brief.core_selling_point,
                "angle": brief.angle,
                "emotion": brief.emotion,
                "audio_mood": brief.audio_mood,
                "beats": [b.name for b in brief.beats],
                "adjustments": brief.adjustments,
            },
        }
        if bgm_track is not None:  # 在线 BGM：带出署名（CC-BY 合规）
            summary["bgm"] = bgm_track.to_dict()
        elif bgm is not None:
            summary["bgm"] = {"provider": "local", "path": str(bgm)}
        self._persist_storyboard(result.name, storyboard)
        self._persist_brief(result.name, brief)

        # 文案写回（标题/文案/话题标签）：复用 ContentService（不再让 pack 生成 segments）
        if generate_content and result.feishu_record_id:
            try:
                ctx = self.content.build_context(
                    plan, language=vo_lang, target_sec=result.duration_sec,
                    emotion=self.voiceover.emotion, country=country_override,
                )
                pack = self.content.generate_pack(ctx, want_segments=False)
                self.content.write_pack(
                    result.feishu_record_id, pack,
                    feishu=feishu_client, table_id=table_id,
                )
                summary["title"] = pack.title
                summary["caption"] = pack.caption
                summary["tags"] = pack.hashtags
            except Exception:  # noqa: BLE001 - 文案失败不影响成片
                logger.exception("文案生成/写回失败（成片已生成）- {}", result.name)

        # 中文版：不再复用越南选材，改为「独立选材+独立剪辑」的完整中文成片，写入「中国」表。
        # 走同一条 Director 链路（produce_cn=False 防递归），失败不阻塞越南版。
        if produce_cn and self.s.cn_twin_enabled:
            try:
                cn = self._produce_cn(
                    product_model=product_model, idx=idx, target=target,
                    tags=tags, upload=upload, generate_content=generate_content,
                )
                if cn:
                    summary["cn"] = cn
            except Exception:  # noqa: BLE001 - 中文版失败不影响越南版
                logger.exception("中文独立版生产失败（越南版已生成）- {}", result.name)
        return summary

    def _cn_render_target(self) -> tuple:
        """返回 (中国成片表 client, table_id)；未配置则 (None, "")。"""
        token = (self.s.feishu_cn_render_app_token or "").strip()
        table = (self.s.feishu_cn_render_table_id or "").strip()
        if not (token and table):
            return None, ""
        return make_feishu_client(token), table

    def _produce_cn(
        self, *, product_model: str, idx: int, target: float,
        tags: list[str], upload: bool, generate_content: bool,
    ) -> dict | None:
        """中文独立版：不复用越南选材，走完整 Director 链路（独立选材+独立剪辑），写「中国」成片表。

        与越南版共用 `_produce_one_director`，只是覆盖市场=zh / 国家=CN / 中文音色 / 写中国表，
        并 produce_cn=False 防止递归再产中文版。
        """
        cn_feishu, cn_table = self._cn_render_target()
        if not cn_feishu:
            logger.info("未配置「中国」成片表（FEISHU_CN_RENDER_*），跳过中文版 - {}", product_model)
            return None

        cn_lang = (self.s.cn_content_language or "zh").strip()
        cn_voice = (self.s.cn_voice_profile or "cn_female_01").strip()

        logger.info("中文独立版开始（独立选材+独立剪辑）- product={}", product_model)
        summary = self._produce_one_director(
            product_model, idx, target, cn_lang, cn_voice, tags, upload, generate_content,
            lambda _p: None,
            market_override=cn_lang, country_override="CN", voice_override=cn_voice,
            feishu_client=cn_feishu, table_id=cn_table,
            name_override=f"{product_model or 'NA'}_{_ts()}_CN",
            produce_cn=False,
        )
        summary["language"] = cn_lang
        logger.info("中文独立版完成 - {} -> record_id={}",
                    summary.get("name"), summary.get("feishu_record_id"))
        return summary

    def _persist_brief(self, name: str, brief) -> None:
        """把 Content Brief 落盘（data/renders/<name>.brief.json），供复盘与后续学习；失败不阻塞。"""
        try:
            out_dir = Path(self.s.data_dir) / "renders"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{name}.brief.json"
            path.write_text(
                json.dumps(brief.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Brief 落盘失败（忽略）- {} err={}", name, exc)

    def _persist_storyboard(self, name: str, storyboard: Storyboard) -> None:
        """把 Storyboard（含解析后的字幕）落盘，供归因/复盘；失败不阻塞。"""
        try:
            out_dir = Path(self.s.data_dir) / "renders"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{name}.storyboard.json"
            path.write_text(
                json.dumps(storyboard.to_dict(include_resolved=True), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Storyboard 落盘失败（忽略）- {} err={}", name, exc)


@lru_cache
def get_produce_service() -> ProduceService:
    return ProduceService(
        selection=get_selection_service(),
        edit=get_edit_service(),
        content=get_content_service(),
        voiceover=get_voiceover_service(),
        settings=get_settings(),
    )
