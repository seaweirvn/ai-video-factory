"""AI 文案服务：为成片生成标题/文案/标签并写回飞书成片表。

- 从成片映射（data/renders/<name>.json）拿到用到的素材，聚合它们的标签作为关键词。
- 交给 ContentProvider（OpenAI / 模板兜底）生成 {title, caption, tags}。
- 写回成片表的 标题/文案/标签 列（缺列自动创建）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from loguru import logger

from adapters.ai_providers import (
    ContentContext,
    ContentPack,
    ContentProvider,
    SceneContext,
    get_content_provider,
)
from adapters.feishu import FeishuBitableClient, make_feishu_client
from app.config import get_settings
from core.enums import MaterialRole
from core.feishu_fields import RENDER_FIELD_TYPES, RENDER_FIELDS
from core.models import Material, RenderPlan
from services.library import (
    MaterialRepository,
    ProductRepository,
    get_material_repository,
    get_product_repository,
)


class ContentService:
    def __init__(
        self,
        provider: ContentProvider,
        repository: MaterialRepository,
        mappings_dir: Path,
        language: str,
        render_feishu: FeishuBitableClient | None = None,
        render_table_id: str = "",
        product_repository: ProductRepository | None = None,
        country: str = "VN",
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.product_repository = product_repository
        self.country = country
        self.mappings_dir = Path(mappings_dir)
        self.language = language
        self.render_feishu = render_feishu
        self.render_table_id = render_table_id
        self._by_record: dict[str, Material] | None = None

    def generate(
        self,
        product_model: str,
        tags: list[str],
        language: str | None = None,
        video_type: str = "",
        role_summary: str = "",
    ) -> dict:
        return self.provider.generate_caption(
            product_model,
            tags,
            language or self.language,
            video_type=video_type,
            role_summary=role_summary,
        )

    def generate_for_render(self, name: str, language: str | None = None) -> dict:
        mapping = self._load_mapping(name)
        product = mapping.get("product_model", "")
        clips = mapping.get("clips", [])
        role_summary = " → ".join(c.get("role_used", "") for c in clips)
        tags = self._aggregate_tags([c.get("record_id", "") for c in clips])
        content = self.generate(product, tags, language, role_summary=role_summary)
        content["context"] = {"product_model": product, "tags": tags, "role_summary": role_summary}
        return content

    def build_context(
        self,
        plan: RenderPlan,
        *,
        language: str | None = None,
        target_sec: float = 25.0,
        emotion: str = "live",
        country: str | None = None,
    ) -> ContentContext:
        """从成片计划的真实片段 + 产品中心，组装 GPT 文案上下文（接地用）。

        主画面默认取 HOOK（定调整片角度），并给出有序 scenes 让口播贴合画面顺序。
        """
        lookup = self._material_lookup()
        scenes: list[SceneContext] = []
        for c in plan.clips:
            m = lookup.get(c.record_id)
            scenes.append(
                SceneContext(
                    role=c.role_used.value,
                    material_type=m.material_type if m else "",
                    shooting_content=m.shooting_content if m else "",
                    main_tag=m.main_tag if m else "",
                    aux_tags=list(m.aux_tags) if m else [],
                )
            )
        primary = next(
            (s for s in scenes if s.role == MaterialRole.hook.value),
            scenes[0] if scenes else SceneContext(),
        )
        secondary: list[str] = []
        for s in scenes:
            for t in s.aux_tags:
                if t and t not in secondary:
                    secondary.append(t)

        profile = None
        if self.product_repository:
            profile = self.product_repository.get(plan.product_model)

        return ContentContext(
            product_model=plan.product_model,
            product_positioning=profile.positioning if profile else "",
            target_audience=profile.target_audience if profile else "",
            product_selling_points=list(profile.selling_points) if profile else [],
            forbidden_words=list(profile.forbidden_words) if profile else [],
            material_type=primary.material_type,
            shooting_content=primary.shooting_content,
            primary_tag=primary.main_tag,
            secondary_tags=secondary[:8],
            scenes=scenes,
            country=country or self.country,
            language=language or self.language,
            target_sec=target_sec,
            emotion=emotion,
        )

    def generate_pack(
        self, ctx: ContentContext, *, want_segments: bool = True
    ) -> ContentPack:
        return self.provider.generate_content_pack(ctx, want_segments=want_segments)

    def write_pack(
        self,
        record_id: str,
        pack: ContentPack,
        *,
        feishu: FeishuBitableClient | None = None,
        table_id: str = "",
    ) -> dict:
        return self.write_to_render(
            record_id,
            {"title": pack.title, "caption": pack.caption, "tags": pack.hashtags},
            feishu=feishu,
            table_id=table_id,
        )

    def write_to_render(
        self,
        record_id: str,
        content: dict,
        *,
        feishu: FeishuBitableClient | None = None,
        table_id: str = "",
    ) -> dict:
        f = feishu or self.render_feishu
        tid = table_id or self.render_table_id
        if not (f and tid):
            raise RuntimeError("未配置成片表（FEISHU_VN_RENDER_APP_TOKEN / _TABLE_ID）")
        values = {
            "title": content.get("title", ""),
            "caption": content.get("caption", ""),
            "tags": " ".join(f"#{t}" for t in content.get("tags", []) if t),
        }
        fields: dict = {}
        for key, val in values.items():
            fname = f.ensure_field(tid, RENDER_FIELDS[key], RENDER_FIELD_TYPES[key])
            fields[fname] = f.format_value(tid, fname, val)
        record = f.update_record(tid, record_id, fields)
        logger.info("文案写回成片表 - record_id={} title={!r}", record_id, values["title"])
        return record

    def generate_and_write(self, name: str, record_id: str, language: str | None = None) -> dict:
        content = self.generate_for_render(name, language)
        content["feishu_record_id"] = self.write_to_render(record_id, content)
        return content

    def _aggregate_tags(self, record_ids: list[str], limit: int = 8) -> list[str]:
        lookup = self._material_lookup()
        seen: list[str] = []
        for rid in record_ids:
            mat = lookup.get(rid)
            if not mat:
                continue
            for tag in mat.tags:
                t = tag.strip()
                if t and t not in seen:
                    seen.append(t)
        return seen[:limit]

    def _material_lookup(self) -> dict[str, Material]:
        if self._by_record is None:
            self._by_record = {m.record_id: m for m in self.repository.load_all()}
        return self._by_record

    def _load_mapping(self, name: str) -> dict:
        path = self.mappings_dir / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"找不到成片映射: {path}")
        return json.loads(path.read_text(encoding="utf-8"))


@lru_cache
def get_content_service() -> ContentService:
    s = get_settings()
    render_feishu = None
    if s.feishu_vn_render_app_token and s.feishu_vn_render_table_id:
        render_feishu = make_feishu_client(s.feishu_vn_render_app_token)
    return ContentService(
        provider=get_content_provider(),
        repository=get_material_repository(),
        mappings_dir=Path(s.data_dir) / "renders",
        language=s.content_language,
        render_feishu=render_feishu,
        render_table_id=s.feishu_vn_render_table_id,
        product_repository=get_product_repository(),
        country=s.content_country,
    )
