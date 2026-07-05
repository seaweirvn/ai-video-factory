"""素材仓库：把飞书素材表读成领域模型 Material 列表。

选材引擎、剪辑等都通过它拿素材，屏蔽飞书字段细节。
"""

from __future__ import annotations

from functools import lru_cache

from loguru import logger

from adapters.feishu import FeishuBitableClient, make_feishu_client
from app.config import get_settings
from core.feishu_fields import MATERIAL_FIELDS
from core.models import Material
from core.roles import parse_roles


class MaterialRepository:
    def __init__(self, feishu: FeishuBitableClient, table_id: str) -> None:
        self.feishu = feishu
        self.table_id = table_id

    def load_all(self, only_ready: bool = True) -> list[Material]:
        if not self.table_id:
            raise RuntimeError("未配置素材表 ID（FEISHU_VN_MATERIAL_TABLE_ID）")
        f = self.feishu
        tid = self.table_id
        link_field = f.resolve_field(tid, MATERIAL_FIELDS["onedrive_link"])
        id_field = f.resolve_field(tid, MATERIAL_FIELDS["material_id"])
        prod_field = f.resolve_field(tid, MATERIAL_FIELDS["product_model"])
        role_field = f.resolve_field(tid, MATERIAL_FIELDS["role"])
        dur_field = f.resolve_field(tid, MATERIAL_FIELDS["duration"])
        main_tag_field = f.resolve_field(tid, MATERIAL_FIELDS["main_tag"])
        aux_tag_field = f.resolve_field(tid, MATERIAL_FIELDS["aux_tags"])

        materials: list[Material] = []
        for record in f.list_records(tid, text_field_as_array=True):
            fields = record.get("fields", {})
            link = f.cell_link(fields.get(link_field)) if link_field else ""
            duration = _to_float(f.cell_text(fields.get(dur_field))) if dur_field else 0.0
            if only_ready and (not link or duration <= 0):
                continue
            roles = parse_roles(f.cell_text(fields.get(role_field))) if role_field else []
            tags = _split_tags(f.cell_text(fields.get(main_tag_field)) if main_tag_field else "")
            tags += _split_tags(f.cell_text(fields.get(aux_tag_field)) if aux_tag_field else "")
            materials.append(
                Material(
                    record_id=record.get("record_id", ""),
                    material_id=f.cell_text(fields.get(id_field)) if id_field else "",
                    product_model=f.cell_text(fields.get(prod_field)) if prod_field else "",
                    roles=roles,
                    tags=tags,
                    onedrive_link=link,
                    duration_sec=duration,
                )
            )
        logger.info("加载素材 {} 条（only_ready={}）", len(materials), only_ready)
        return materials


def _to_float(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _split_tags(text: str) -> list[str]:
    return [t.strip() for t in str(text).replace("，", " ").replace(",", " ").split() if t.strip()]


@lru_cache
def get_material_repository() -> MaterialRepository:
    s = get_settings()
    app_token = s.feishu_vn_material_app_token or s.feishu_vn_bitable_app_token
    return MaterialRepository(make_feishu_client(app_token), s.feishu_vn_material_table_id)
