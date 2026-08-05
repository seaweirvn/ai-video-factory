"""CaptionGenerator：按「国家/语言/阶段」从 Prompt Library 读 prompt，生成成交字幕。

- 不在代码里写死任何字幕文案；所有语气/规则/示例都来自 prompt_library/*.yaml。
- 语言回落：language -> en -> default（见 prompt.load_prompt）。
- 生成结果落 jsonl 日志（data/logs/caption_generation.jsonl），便于后续按
  country/stage/selling_point 对比 CTR/CVR/GMV。
- LLM 不可用或失败时安全回落到 prompt 里的 good_examples，绝不抛异常打断剪辑。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import httpx
from loguru import logger

from app.config import get_settings
from services.caption import prompt as pl


def _apply_vars(text: str, variables: dict | None) -> str:
    if not text or not variables:
        return text or ""
    out = text
    for k, v in variables.items():
        out = out.replace("{" + str(k) + "}", str(v))
    return out


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
    return t.strip()


class CaptionGenerator:
    def __init__(self, settings=None) -> None:
        self.s = settings or get_settings()
        self._log_path = Path(self.s.data_dir) / "logs" / "caption_generation.jsonl"

    @property
    def _model(self) -> str:
        """字幕/口播文案模型：优先用更强的 caption 专用模型，缺省回落通用 openai_model。"""
        return (getattr(self.s, "openai_caption_model", "") or self.s.openai_model).strip()

    # ---- public API ------------------------------------------------------
    def generate(
        self,
        *,
        product: str,
        language: str,
        main_selling_point: str,
        stage: str,
        country: str = "",
        target: str = "conversion",
        variables: dict | None = None,
        caption_style: str = "",
    ) -> dict:
        """生成单条字幕。返回 {stage, language, caption, ...meta}。任何异常都安全兜底。"""
        stage = pl.stage_for_intent(stage)  # 兼容传 intent_type 或 stage 名
        country = country or pl.country_name(language)
        variables = {"product_name": product, **(variables or {})}
        prompt_cfg, used_lang = pl.load_prompt(language, stage)

        caption, source = "", "empty"
        try:
            if prompt_cfg:
                caption = self._call_llm(
                    prompt_cfg, product=product, language=language, used_lang=used_lang,
                    country=country, main_selling_point=main_selling_point, stage=stage,
                    target=target, caption_style=caption_style,
                )
                if caption:
                    source = "llm"
            if not caption:
                caption = self._example_fallback(prompt_cfg, variables)
                source = "example" if caption else "empty"
        except Exception as exc:  # noqa: BLE001 - 字幕失败绝不打断剪辑
            logger.warning("CaptionGenerator 生成异常(已吞) - {}", exc)
            caption = self._example_fallback(prompt_cfg, variables)
            source = "example" if caption else "empty"

        caption = _apply_vars(caption, variables).strip()
        result = {
            "stage": stage,
            "language": language,
            "caption": caption,
            "product": product,
            "country": country,
            "main_selling_point": main_selling_point,
            "target": target,
            "caption_style": caption_style,
            "prompt_lang_used": used_lang,
            "source": source,
        }
        self._log(result)
        return result

    def generate_stage_script(
        self,
        *,
        product: str,
        language: str,
        main_selling_point: str,
        stage: str,
        slot_sec: float,
        country: str = "",
        target: str = "conversion",
        variables: dict | None = None,
        caption_style: str = "",
        context: str = "",
        avoid_lines: list[str] | None = None,
    ) -> dict:
        """为一个阶段生成「填满时间槽的多句脚本」。返回 {stage, language, captions:[...], ...}。

        句数/字数按 slot_sec 估算（念白略短于 slot，交给合成端补齐到精确 slot），
        让字幕连续铺满而非一句短模板。任何异常都安全兜底，绝不打断剪辑。
        avoid_lines：本视频前面各段已生成的字幕；喂给 LLM 让它换词换角度，避免同一个词
        （如"劲大"）反复出现。
        """
        stage = pl.stage_for_intent(stage)
        country = country or pl.country_name(language)
        variables = {"product_name": product, **(variables or {})}
        prompt_cfg, used_lang = pl.load_prompt(language, stage)
        n_lines = self._plan_line_count(stage, slot_sec)
        word_budget = self._plan_word_budget(stage, slot_sec)

        captions: list[str] = []
        source = "empty"
        try:
            if prompt_cfg:
                captions = self._call_llm_script(
                    prompt_cfg, product=product, language=language, used_lang=used_lang,
                    country=country, main_selling_point=main_selling_point, stage=stage,
                    target=target, caption_style=caption_style,
                    n_lines=n_lines, word_budget=word_budget, context=context,
                    avoid_lines=avoid_lines,
                )
                if captions:
                    source = "llm"
            if not captions:
                captions = self._example_lines(prompt_cfg, n_lines, variables)
                source = "example" if captions else "empty"
        except Exception as exc:  # noqa: BLE001 - 字幕失败绝不打断剪辑
            logger.warning("CaptionGenerator 段脚本生成异常(已吞) - {}", exc)
            captions = self._example_lines(prompt_cfg, n_lines, variables)
            source = "example" if captions else "empty"

        # 字幕即口播文本：语音永远读字幕这句话（品牌/型号/单位的「读音」由 Speech Formatter 本地化），
        # 保证「看到的」和「听到的」是同一句话，只在品牌读法上不同（S2 显示英文、读作 Ét Hai）。
        captions = [
            _apply_vars(c, variables).strip() for c in captions if c and str(c).strip()
        ]
        result = {
            "stage": stage,
            "language": language,
            "captions": captions,
            "product": product,
            "country": country,
            "main_selling_point": main_selling_point,
            "target": target,
            "slot_sec": round(float(slot_sec or 0.0), 2),
            "n_lines": n_lines,
            "word_budget": word_budget,
            "prompt_lang_used": used_lang,
            "source": source,
        }
        self._log(result)
        return result

    @staticmethod
    def _plan_line_count(stage: str, slot_sec: float) -> int:
        """按阶段与时间槽定句数：Hook 1（强钩），其余随 slot 增加。"""
        slot = max(0.0, float(slot_sec or 0.0))
        if stage == "hook":
            return 1
        if stage == "cta":
            return max(1, min(2, round(slot / 3.0)))
        if stage == "value":
            return max(2, min(3, round(slot / 3.0)))
        if stage == "proof":
            return max(3, min(4, round(slot / 3.0)))
        return max(1, round(slot / 3.0))

    @staticmethod
    def _plan_word_budget(stage: str, slot_sec: float) -> int:
        """整段目标词数：念白约 2.6 词/秒，保守取值让念白略短于 slot。Hook 封顶 8 词。"""
        slot = max(0.0, float(slot_sec or 0.0))
        if stage == "hook":
            return 8
        return max(6, int(slot * 2.2))

    # ---- internals -------------------------------------------------------
    def _call_llm(
        self, cfg: dict, *, product: str, language: str, used_lang: str, country: str,
        main_selling_point: str, stage: str, target: str, caption_style: str,
    ) -> str:
        if not self.s.openai_api_key:
            return ""  # 无 key -> 交给示例兜底
        system, user = self._build_messages(
            cfg, product=product, language=language, used_lang=used_lang, country=country,
            main_selling_point=main_selling_point, stage=stage, target=target,
            caption_style=caption_style,
        )
        resp = httpx.post(
            f"{self.s.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.9,
                "response_format": {"type": "json_object"},
            },
            timeout=40.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(_strip_fences(content))
        return str(data.get("caption") or "").strip().splitlines()[0][:120] if data else ""

    @staticmethod
    def _build_messages(
        cfg: dict, *, product: str, language: str, used_lang: str, country: str,
        main_selling_point: str, stage: str, target: str, caption_style: str,
    ) -> tuple[str, str]:
        # 输出语言永远 = 请求的目标市场语言（used_lang 只决定用哪套 prompt 模板/规则/示例）。
        # 这样即便某语言没有专属 prompt、逐段回落到 en 或 default，输出仍是目标语言，
        # 不会出现「同一条视频里中文/英文混排」（多国扩展直接生效）。
        out_lang = language or used_lang
        lang_name = pl.language_name(out_lang)
        rules = "\n".join(f"- {r}" for r in (cfg.get("rules") or []))
        good = "\n".join(f"- {g}" for g in (cfg.get("good_examples") or []))
        bad = "\n".join(f"- {b}" for b in (cfg.get("bad_examples") or []))
        output_format = _apply_vars(
            str(cfg.get("output_format") or 'Return ONLY JSON: {"caption": "..."}'),
            {"language_name": lang_name},
        )
        system = (
            f"You are a native {lang_name} short-video conversion copywriter for {country}. "
            f"You write TikTok-style selling captions for the '{stage}' stage of a "
            f"Hook -> Value -> Proof -> CTA product video. "
            f"Goal of this stage: {cfg.get('goal') or ''}. "
            f"Tone: {cfg.get('tone') or ''}. "
            "Write native, non-translationese copy. Output strict JSON only."
        )
        style_line = f"\nExtra style: {caption_style}" if caption_style else ""
        user = (
            f"product: {product}\n"
            f"country: {country}\n"
            f"language: {lang_name}\n"
            f"main_selling_point: {main_selling_point}\n"
            f"video_stage: {stage}\n"
            f"target: {target}{style_line}\n\n"
            f"RULES:\n{rules or '- (none)'}\n\n"
            f"GOOD EXAMPLES (style reference, do not copy):\n{good or '- (none)'}\n\n"
            f"BAD EXAMPLES (never do this):\n{bad or '- (none)'}\n\n"
            f"{output_format}"
        )
        return system, user

    def _call_llm_script(
        self, cfg: dict, *, product: str, language: str, used_lang: str, country: str,
        main_selling_point: str, stage: str, target: str, caption_style: str,
        n_lines: int, word_budget: int, context: str = "",
        avoid_lines: list[str] | None = None,
    ) -> list[str]:
        """返回该段 n_lines 句字幕文本；此文本同时用于配音（读法由 Speech Formatter 本地化）。"""
        if not self.s.openai_api_key:
            return []
        # 输出语言永远 = 请求的目标市场语言（见 _build_messages 注释）：避免中英混排。
        out_lang = language or used_lang
        lang_name = pl.language_name(out_lang)
        rules = "\n".join(f"- {r}" for r in (cfg.get("rules") or []))
        good = "\n".join(f"- {g}" for g in (cfg.get("good_examples") or []))
        bad = "\n".join(f"- {b}" for b in (cfg.get("bad_examples") or []))
        system = (
            f"You are a native {lang_name} short-video conversion copywriter for {country}. "
            f"You write the spoken lines for the '{stage}' stage of a "
            f"Hook -> Value -> Proof -> CTA product video that must SELL. "
            f"Goal of this stage: {cfg.get('goal') or ''}. "
            f"Tone: {cfg.get('tone') or ''}. "
            "Write native, spoken, non-translationese lines. Output strict JSON only."
        )
        style_line = f"\nExtra style: {caption_style}" if caption_style else ""
        # 产品上下文接地：把生成钉死在真实产品品类，杜绝跑偏到软件/APP 等其它领域
        ctx_block = (
            "PRODUCT CONTEXT (this is a REAL physical product; ground EVERY line in THIS exact "
            "product and its real-world category and usage; NEVER drift to software/apps/data/"
            "screens or any other domain):\n" + context.strip() + "\n\n"
        ) if context and context.strip() else ""
        style_block = self._style_block(out_lang)
        # 跨段去重：把前面各段已说过的话喂进来，强制换词换角度（避免"劲大"这类词反复出现）
        avoid = [str(x).strip() for x in (avoid_lines or []) if str(x).strip()]
        avoid_block = (
            "ALREADY SAID earlier in THIS SAME video (do NOT repeat these lines, and do NOT "
            "reuse the same adjective/keyword such as a repeated 劲大/顺滑 — vary the wording "
            "and come at the selling point from a fresh, concrete angle):\n"
            + "\n".join(f"- {a}" for a in avoid[-12:]) + "\n\n"
        ) if avoid else ""
        user = (
            f"product: {product}\n"
            f"country: {country}\n"
            f"language: {lang_name}\n"
            f"main_selling_point: {main_selling_point}\n"
            f"video_stage: {stage}\n"
            f"target: {target}{style_line}\n\n"
            f"{style_block}"
            f"{ctx_block}"
            f"{avoid_block}"
            f"Write EXACTLY {n_lines} short spoken line(s) that flow as one mini-script for "
            f"this stage, each line advancing the story (no repetition, no numbering, no emojis "
            f"unless natural). Keep EACH line short (about 6-9 words, ~3 seconds to say). "
            f"Total about {word_budget} words across all lines, so it reads aloud within the "
            f"stage time (do not exceed it). Stay on the single main_selling_point, but express "
            f"it in PLAIN, concrete words a normal buyer instantly understands — say the real "
            f"benefit the user feels (e.g. big fish still reel in easily), NOT a vague terse "
            f"adjective on its own.\n\n"
            f"This SAME text is BOTH the on-screen subtitle AND the spoken voiceover, so viewers "
            f"read exactly what they hear. Keep brand names, model numbers, English words and units "
            f'EXACTLY as written (e.g. "SEAWEIR S2", "250g") — do NOT phonetically respell them; a '
            f"downstream pronunciation layer reads them naturally in {lang_name}.\n\n"
            f"RULES:\n{rules or '- (none)'}\n\n"
            f"GOOD EXAMPLES (style reference, do not copy):\n{good or '- (none)'}\n\n"
            f"BAD EXAMPLES (never do this):\n{bad or '- (none)'}\n\n"
            f'Return ONLY JSON: {{"captions": ["...", "..."]}} — '
            f"exactly {n_lines} line(s) in {lang_name}."
        )
        resp = httpx.post(
            f"{self.s.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.s.openai_api_key}"},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.85,
                "response_format": {"type": "json_object"},
            },
            timeout=40.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(_strip_fences(content))
        raw = data.get("captions") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            single = str((data or {}).get("caption") or "").strip()
            raw = [single] if single else []
        out: list[str] = []
        for x in raw:
            s = str(x).strip()
            if s:
                out.append(s[:120])
        return out

    @staticmethod
    def _style_block(out_lang: str) -> str:
        """把某语言的「口语风格 + 术语表 + 禁用词」拼成强约束块，喂给 LLM（缺则空串）。"""
        style = pl.load_style(out_lang)
        if not style:
            return ""
        parts: list[str] = []
        spoken = str(style.get("spoken_style") or "").strip()
        if spoken:
            parts.append("SPOKEN STYLE (imitate this exact voice):\n" + spoken)
        glossary = [str(x).strip() for x in (style.get("glossary") or []) if str(x).strip()]
        if glossary:
            parts.append(
                "DOMAIN GLOSSARY (use EXACTLY these native terms; never invent or mistranslate "
                "jargon):\n" + "\n".join(f"- {g}" for g in glossary)
            )
        must = [str(x).strip() for x in (style.get("must") or []) if str(x).strip()]
        if must:
            parts.append("MUST:\n" + "\n".join(f"- {m}" for m in must))
        forbidden = [str(x).strip() for x in (style.get("forbidden") or []) if str(x).strip()]
        if forbidden:
            parts.append(
                "FORBIDDEN (never output any of these):\n"
                + "\n".join(f"- {b}" for b in forbidden)
            )
        return ("\n\n".join(parts) + "\n\n") if parts else ""

    @staticmethod
    def _example_fallback(cfg: dict, variables: dict | None) -> str:
        examples = cfg.get("good_examples") if isinstance(cfg, dict) else None
        if isinstance(examples, list):
            for ex in examples:
                if ex and str(ex).strip():
                    return _apply_vars(str(ex).strip(), variables)
        return ""

    @staticmethod
    def _example_lines(cfg: dict, n: int, variables: dict | None) -> list[str]:
        """无 LLM 时用 prompt 的 good_examples 兜底铺满 n 句（不足则循环，去重优先）。"""
        pool = [str(x).strip() for x in (cfg.get("good_examples") or []) if x and str(x).strip()] \
            if isinstance(cfg, dict) else []
        if not pool:
            return []
        out: list[str] = []
        i = 0
        while len(out) < max(1, n):
            out.append(_apply_vars(pool[i % len(pool)], variables))
            i += 1
            if i >= len(pool) and len(out) >= len(pool):
                break
        return out

    def _log(self, result: dict) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            row = {"ts": datetime.now(timezone.utc).isoformat(), "model": self.s.openai_model, **result}
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 日志失败不影响生成
            logger.warning("字幕生成日志写入失败(忽略) - {}", exc)


@lru_cache(maxsize=1)
def get_caption_generator() -> CaptionGenerator:
    return CaptionGenerator()
