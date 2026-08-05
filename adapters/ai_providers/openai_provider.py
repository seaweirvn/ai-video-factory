"""OpenAI 兼容的文案 provider（chat/completions），支持 GPT/DeepSeek/本地端点。

只用 httpx 直接打 /chat/completions，避免绑定特定 SDK，方便切换端点。
输出强制 JSON：{title, caption, tags}。
"""

from __future__ import annotations

import json

import httpx
from loguru import logger

from adapters.ai_providers.base import (
    ContentContext,
    ContentPack,
    ContentProvider,
    ScriptSegment,
    TemplateContentProvider,
)

# 禁用语气词/空泛口头语：避免脚本听起来像模板化主播喊麦（越南语专用）。
_BANNED_FILLERS = "Ê, Nè, Ủa, Đó, Anh em ơi, Trời ơi, Ghê, Đúng luôn, Xịn"

_SPOKEN_SYSTEM = (
    "You are a real Vietnamese TikTok livestream host (người bán hàng livestream). "
    "You DO NOT read scripts. You talk to the camera casually, punchy and fast, "
    "the way Vietnamese streamers hype products live. Output strict JSON only."
)


def _content_system(lang_name: str) -> str:
    """内容包（标题/文案/标签/口播）系统提示：按语言切换主播人设，不再写死越南。

    卖点驱动的本土带货主播（TikTok/抖音/快手风格），画面只作证明（Proof）。
    """
    return (
        f"You are a real {lang_name} short-video live-selling host "
        "(TikTok / Douyin / Kuaishou style). "
        "Your ONLY goal is to make people BUY. "
        "You NEVER narrate, describe or explain what is on screen like a documentary. "
        "You LEAD with selling points and use the footage only as PROOF of those points. "
        "Every single line has a selling purpose (hook, benefit, proof, urgency, CTA). "
        f"You talk in short, punchy, emotional NATIVE spoken {lang_name} with rhythm and urgency, "
        "like a real streamer hyping a product, never like an article, a manual, or a translation. "
        "Output strict JSON only."
    )


def _style_block(lang_code: str) -> str:
    """把某语言的「口语风格 + 术语表 + 禁用词」拼成强约束块喂给 LLM（缺则空串）。

    复用字幕/口播用的 prompt_library/{lang}/_style.yaml，标题/文案/口播共用同一套本土化约束。
    懒加载 services 层，避免 adapters 层 import 期耦合。
    """
    try:
        from services.caption.prompt import load_style
    except Exception:  # noqa: BLE001 - 缺依赖不影响文案生成
        return ""
    style = load_style(lang_code)
    if not style:
        return ""
    parts: list[str] = []
    spoken = str(style.get("spoken_style") or "").strip()
    if spoken:
        parts.append("SPOKEN STYLE (imitate this exact native voice):\n" + spoken)
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
            "FORBIDDEN (never output any of these):\n" + "\n".join(f"- {b}" for b in forbidden)
        )
    return ("\n\n".join(parts) + "\n\n") if parts else ""

_LANG_NAME = {
    "vi": "Vietnamese",
    "th": "Thai",
    "ms": "Malay",
    "id": "Indonesian",
    "en": "English",
    "zh": "Chinese",
}


def _theme_budget(target_sec: float) -> tuple[int, int]:
    """按视频时长控制主题和卖点密度。"""
    sec = max(10.0, float(target_sec or 20.0))
    if sec < 25:
        return 2, 4
    return 3, 6


_SYSTEM = (
    "You are a senior TikTok short-video copywriter for e-commerce. "
    "Write native, catchy, platform-native copy that drives views and conversions. "
    "Always answer with a strict JSON object only."
)


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    return text.strip()


class OpenAIContentProvider(ContentProvider):
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 40.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._fallback = TemplateContentProvider()

    def generate_caption(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        video_type: str = "",
        role_summary: str = "",
    ) -> dict:
        lang = _LANG_NAME.get(language.lower(), language)
        prompt = self._build_prompt(product_model, tags, lang, video_type, role_summary)
        try:
            content = self._chat(prompt)
            data = self._parse(content)
            data.setdefault("tags", tags)
            return data
        except Exception as exc:
            logger.warning("OpenAI 文案生成失败，降级模板 - {}", exc)
            return self._fallback.generate_caption(
                product_model, tags, language, video_type=video_type, role_summary=role_summary
            )

    def generate_script(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        target_sec: float = 25.0,
    ) -> list[str]:
        lang = _LANG_NAME.get(language.lower(), language)
        n = max(2, min(8, round(target_sec / 4)))  # 约每句 4 秒
        prompt = (
            f"Write a spoken TikTok voiceover script in {lang} for product '{product_model}'.\n"
            f"Target length about {int(target_sec)} seconds, ~{n} short sentences.\n"
            f"Keywords: {', '.join(tags) or 'none'}.\n"
            "Structure: a strong hook, 1-2 value/proof points, a clear call to action.\n"
            "Sentences must be short and natural to read aloud.\n"
            'Return ONLY JSON: {"sentences": ["...", "..."]} '
            f"with each sentence in {lang}."
        )
        try:
            content = self._chat(prompt)
            data = json.loads(_strip_fences(content))
            sentences = [str(s).strip() for s in data.get("sentences", []) if str(s).strip()]
            if sentences:
                return sentences
        except Exception as exc:
            logger.warning("OpenAI 口播脚本生成失败，降级模板 - {}", exc)
        return self._fallback.generate_script(product_model, tags, language, target_sec=target_sec)

    def generate_spoken_script(
        self,
        product_model: str,
        tags: list[str],
        language: str = "vi",
        *,
        target_sec: float = 25.0,
        emotion: str = "live",
    ) -> list[ScriptSegment]:
        lang = _LANG_NAME.get(language.lower(), language)
        n = max(4, min(12, round(target_sec / 2.2)))  # 主播口语句子短，句数更多
        prompt = self._build_spoken_prompt(product_model, tags, lang, n, emotion)
        try:
            content = self._chat(prompt, system=_SPOKEN_SYSTEM)
            data = json.loads(_strip_fences(content))
            segments = self._parse_segments(data)
            if segments:
                return segments
        except Exception as exc:
            logger.warning("OpenAI 主播口语脚本失败，降级默认包装 - {}", exc)
        return super().generate_spoken_script(
            product_model, tags, language, target_sec=target_sec, emotion=emotion
        )

    def generate_content_pack(
        self, ctx: ContentContext, *, want_segments: bool = True
    ) -> ContentPack:
        lang = _LANG_NAME.get(ctx.language.lower(), ctx.language)
        n = max(4, min(12, round(ctx.target_sec / 2.2)))
        prompt = self._build_content_prompt(ctx, lang, n, want_segments)
        try:
            content = self._chat(prompt, system=_content_system(lang))
            data = json.loads(_strip_fences(content))
            pack = self._parse_pack(data, ctx, want_segments)
            if pack.title or pack.caption or pack.segments:
                return pack
        except Exception as exc:  # noqa: BLE001 - 失败降级到默认拼装
            logger.warning("OpenAI 内容包生成失败，降级默认拼装 - {}", exc)
        return super().generate_content_pack(ctx, want_segments=want_segments)

    def _build_content_prompt(
        self, ctx: ContentContext, lang: str, n: int, want_segments: bool
    ) -> str:
        topic_count, selling_point_count = _theme_budget(ctx.target_sec)
        gpt_input = {
            "product_model": ctx.product_model,
            "product_positioning": ctx.product_positioning,
            "target_audience": ctx.target_audience,
            "product_selling_points": ctx.product_selling_points,
            "material_type": ctx.material_type,
            "shooting_content": ctx.shooting_content,
            "primary_tag": ctx.primary_tag,
            "secondary_tags": ctx.secondary_tags,
            "forbidden_words": ctx.forbidden_words,
            "country": ctx.country,
            "language": ctx.language,
            "target_duration_sec": ctx.target_sec,
            "topic_budget": {
                "core_topics": 1,
                "auxiliary_topics": max(0, topic_count - 1),
                "total_topics": topic_count,
                "selling_points": selling_point_count,
            },
            "scenes": [
                {
                    "role": s.role,
                    "material_type": s.material_type,
                    "shooting_content": s.shooting_content,
                    "primary_tag": s.main_tag,
                    "secondary_tags": s.aux_tags,
                }
                for s in ctx.scenes
            ],
        }
        lang_code = ctx.language.lower()
        is_vi = lang_code == "vi"
        style_block = _style_block(lang_code)

        vi_hooks = (
            " Good hooks: 'Quay mượt cực luôn!', 'Drag ổn định thật sự!', "
            "'Giá này quá hời!', 'Cối kim loại quá đáng tiền!'."
            if is_vi else ""
        )
        vi_cta = (
            " (e.g. 'Chốt luôn nhé!', 'Nhắm mắt mua đi anh em!')" if is_vi else ""
        )
        filler_rule = (
            f"- Do NOT use empty filler words or standalone particles: {_BANNED_FILLERS}.\n"
            if is_vi
            else "- Do NOT use empty filler words, generic greetings, or hollow ad-clichés. "
            "Start directly with a concrete product benefit.\n"
        )
        seg_rules = (
            f"- segments: EXACTLY {n} spoken lines, each 5-12 words, one idea per line. "
            "Do not return fewer lines; this controls the final video length.\n"
            f"- Spoken NATIVE {lang} sales copy, NOT written/formal, but keep it clean "
            "and benefit-led.\n"
            + filler_rule
            + "- SCRIPT SHAPE (every line must sell, never narrate):\n"
            "  line 1 = a strong product-led HOOK from product_selling_points or the strongest "
            "visible selling point. DO NOT start with a generic greeting as a standalone line."
            + vi_hooks + "\n"
            "  line 2 = state the CORE TOPIC's concrete selling point.\n"
            "  line 3 = PROVE the core topic using what the footage happens to show, as a BENEFIT "
            "claim, NOT a description.\n"
            "  middle lines = reinforce / stack more selling points + urgency.\n"
            "  last line = a strong CTA" + vi_cta + ".\n"
            "- Each line: 'pause' after it in ms (100/200/300/500; longer after the hook & "
            "before the CTA), 'emotion' one of normal|happy|excited|review|live (default live; "
            "hook/CTA usually excited), optional 'type' (statement|question|exclaim|cta|call) "
            "and optional 'emphasis' (1-3 key selling words).\n"
            if want_segments
            else "- segments: return an empty array [].\n"
        )
        seg_example = (
            (
                '"segments":[{"text":"Quay mượt cực luôn!","pause":300,"emotion":"excited","type":"exclaim",'
                '"emphasis":["mượt cực"]},'
                '{"text":"Con này thu dây về rất nhẹ","pause":200,"emotion":"excited","type":"statement",'
                '"emphasis":["mượt cực"]},'
                '{"text":"Thấy chưa, một cái là dây về liền","pause":250,"emotion":"live","type":"statement",'
                '"emphasis":[]},'
                '{"text":"Chốt luôn nha anh em!","pause":0,"emotion":"excited","type":"cta","emphasis":[]}]'
                if is_vi
                else '"segments":[{"text":"...","pause":300,"emotion":"excited","type":"exclaim",'
                '"emphasis":["..."]},{"text":"...","pause":0,"emotion":"excited","type":"cta",'
                '"emphasis":[]}]'
            )
            if want_segments
            else '"segments":[]'
        )
        conversion_examples = (
            "CONVERSION EXAMPLES (wrong = narration, right = selling):\n"
            "- high-speed reeling: WRONG 'Giờ xem tốc độ thu dây.' | "
            "RIGHT 'Thu dây mượt cực luôn!', 'Một cái là về liền!', 'Cảm giác đã cực!'\n"
            "- catching a fish: WRONG 'Mình câu được một con cá.' | "
            "RIGHT 'Thấy chưa? Kéo cá nhẹ tênh!', 'Đây là sức mạnh ổn định!'\n"
            "- handheld showcase: WRONG 'Giờ mình show sản phẩm.' | "
            "RIGHT 'Giá này quá đáng tiền!', 'Người mới nhắm mắt mua luôn!', "
            "'Cây đầu tiên chọn nó là đủ!'\n"
            if is_vi
            else "CONVERSION PRINCIPLE (wrong = narration, right = selling): never say "
            "'now watch the fast retrieve' (narration); instead turn it into a benefit the "
            "buyer feels, in native everyday spoken language.\n"
        )
        return (
            "INPUT (JSON):\n"
            + json.dumps(gpt_input, ensure_ascii=False)
            + "\n\n"
            + style_block
            + f"Write TikTok LIVE-SELLING content in NATIVE {lang} (sound like a local host, "
            "never like a translation).\n"
            "CORE PRINCIPLE: selling-point driven, NOT footage driven. "
            "The footage is only PROOF. NEVER describe or explain the scene. "
            "Turn what the footage shows into a persuasive BENEFIT that makes people want to buy.\n"
            + conversion_examples
            + "TOPIC PLANNING:\n"
            f"- Use exactly 1 core topic + 0-2 auxiliary topics. For this video target: "
            f"{topic_count} total topics and {selling_point_count} total selling points.\n"
            "- Core topic MUST come from shooting_content + primary_tag (what this footage proves best).\n"
            "- Auxiliary topics MAY come from secondary_tags, product_selling_points, user benefits, "
            "recommended marketing angles, FAQ-like objections, or brand keywords.\n"
            "- The goal is NOT rigid topic consistency. The goal is higher conversion rate.\n"
            "- But every auxiliary topic must be reasonably supported by the current footage/tags. "
            "If the footage cannot support it, do not use it.\n"
            "- 20s video: about 2 topics, 3-4 selling points. 30s video: about 3 topics, 4-6 selling points.\n"
            "RULES:\n"
            "1. Lead with the selling angle: primary_tag = the #1 point; secondary_tags = extra points.\n"
            "2. Use shooting_content ONLY to know what proof is available; convert it to a benefit, "
            "never narrate it.\n"
            "3. Only claim points the footage/tags can actually back up. "
            "NEVER invent a feature that is not shown or tagged.\n"
            "4. product_selling_points is the preferred source for HOOK and supporting sales claims. "
            "Use only points that do not conflict with footage/tags.\n"
            "5. product_positioning / target_audience = background flavor only, must NOT take over.\n"
            "6. NEVER use any of the forbidden_words.\n"
            "7. Every single line must have a selling purpose. No documentary, no news, no manual.\n"
            + (
                "8. Do not use generic openings or filler particles such as 'Anh em ơi!' / "
                "'Nè anh em!' / 'Trời ơi' / 'Ghê'. Start directly with a product benefit.\n"
                if is_vi
                else "8. Do not use generic openings, filler particles, or hollow marketing "
                "clichés. Start directly with a concrete product benefit in native everyday words.\n"
            )
            + seg_rules
            + "- title: a punchy selling hook (<= 40 chars). caption: 1-2 selling sentences. "
            "hashtags: 5-10 tag strings WITHOUT the # sign.\n"
            f"- ALL text must be in {lang}.\n"
            "Return ONLY JSON:\n"
            '{"title":"","caption":"","hashtags":[],' + seg_example + "}"
        )

    def _parse_pack(
        self, data: dict, ctx: ContentContext, want_segments: bool
    ) -> ContentPack:
        title = str(data.get("title", "")).strip()
        caption = str(data.get("caption", "")).strip()
        hashtags_raw = data.get("hashtags") or data.get("tags") or []
        if isinstance(hashtags_raw, str):
            hashtags_raw = [hashtags_raw]
        hashtags = [str(t).lstrip("#").strip() for t in hashtags_raw if str(t).strip()]
        if not hashtags:
            hashtags = ctx.keyword_tags()
        segments = self._parse_segments(data) if want_segments else []
        return ContentPack(
            title=title, caption=caption, hashtags=hashtags, segments=segments
        )

    @staticmethod
    def _parse_segments(data: dict) -> list[ScriptSegment]:
        raw = data.get("segments") or []
        segments: list[ScriptSegment] = []
        for item in raw:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    segments.append(
                        ScriptSegment(
                            text=text,
                            pause_ms=ScriptSegment.infer_pause(text),
                            kind=ScriptSegment.infer_kind(text),
                        )
                    )
                continue
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            kind = str(item.get("type") or item.get("kind") or "").strip().lower()
            if kind not in {"statement", "question", "exclaim", "cta", "call"}:
                kind = ScriptSegment.infer_kind(text)
            try:
                pause = int(item.get("pause", 0))
            except (TypeError, ValueError):
                pause = 0
            if pause <= 0:
                pause = ScriptSegment.infer_pause(text)
            pause = max(0, min(1200, pause))
            emphasis_raw = item.get("emphasis") or []
            if isinstance(emphasis_raw, str):
                emphasis_raw = [emphasis_raw]
            emphasis = [str(e).strip() for e in emphasis_raw if str(e).strip()]
            emotion = str(item.get("emotion") or "").strip().lower()
            if emotion not in {"normal", "happy", "excited", "review", "live"}:
                emotion = ""
            segments.append(
                ScriptSegment(
                    text=text, pause_ms=pause, emphasis=emphasis, kind=kind, emotion=emotion
                )
            )
        return segments

    def _build_spoken_prompt(
        self, product_model: str, tags: list[str], lang: str, n: int, emotion: str
    ) -> str:
        return (
            f"Product: '{product_model}'. Keywords: {', '.join(tags) or 'none'}.\n"
            f"Write a LIVE {lang} TikTok host monologue selling this product, "
            f"mood = {emotion}.\n"
            "STRICT RULES:\n"
            f"- About {n} very short sentences (spoken lines), each 5-12 words.\n"
            "- One idea per sentence. NO long sentences. NO written/formal style.\n"
            "- Sound like clean spoken Vietnamese sales copy, not an article or manual.\n"
            f"- Do NOT use empty filler words or standalone particles: {_BANNED_FILLERS}.\n"
            "- Flow: hook the viewer, show value/proof, then a strong call to action.\n"
            "- For each line give a natural pause AFTER it in ms "
            "(100/200/300/500; longer after hooks & before the CTA).\n"
            "- Mark 1-3 key words to stress in 'emphasis' "
            "(e.g. 'mượt cực', 'giá quá hời', 'rất đáng mua').\n"
            "- 'type' is one of: statement | question | exclaim | cta | call.\n"
            "Return ONLY JSON:\n"
            '{"segments":[{"text":"Quay mượt cực luôn","pause":300,"emphasis":["mượt cực"],"type":"exclaim"},'
            '{"text":"Thu dây nhẹ và rất đã tay","pause":200,"emphasis":["nhẹ"],"type":"statement"}]}'
        )

    def _build_prompt(
        self, product_model: str, tags: list[str], lang: str, video_type: str, role_summary: str
    ) -> str:
        return (
            f"Write TikTok copy in {lang} for product model '{product_model}'.\n"
            f"Video type: {video_type or 'product showcase'}.\n"
            f"Video structure: {role_summary or 'HOOK + VALUE/PROOF + CTA'}.\n"
            f"Relevant tags/keywords: {', '.join(tags) or 'none'}.\n\n"
            "Return ONLY a JSON object with keys:\n"
            '  "title": a short punchy title (<= 40 chars),\n'
            '  "caption": 1-2 sentence caption ending with 4-8 relevant hashtags,\n'
            '  "tags": array of 5-10 hashtag strings without the # sign.\n'
            f"All text must be in {lang}."
        )

    def _chat(self, prompt: str, system: str = _SYSTEM) -> str:
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.9,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _parse(content: str) -> dict:
        data = json.loads(_strip_fences(content))
        return {
            "title": str(data.get("title", "")).strip(),
            "caption": str(data.get("caption", "")).strip(),
            "tags": [str(t).lstrip("#").strip() for t in data.get("tags", []) if str(t).strip()],
        }
