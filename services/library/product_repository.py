"""产品中心仓库：把飞书产品中心表读成 ProductProfile（定位/人群/禁用词）。

用于给 GPT 文案做「背景信息接地」。未配置产品中心表时优雅降级：
get(product_model) 返回 None，文案链路照常跑，只是少了产品背景。
"""

from __future__ import annotations

from functools import lru_cache

from loguru import logger

from adapters.feishu import FeishuBitableClient, make_feishu_client
from app.config import get_settings
from core.feishu_fields import PRODUCT_FIELDS
from core.models import ProductProfile


class ProductRepository:
    def __init__(self, feishu: FeishuBitableClient | None, table_id: str) -> None:
        self.feishu = feishu
        self.table_id = table_id
        self._cache: dict[str, ProductProfile] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.feishu and self.table_id)

    def get(self, product_model: str) -> ProductProfile | None:
        if not product_model:
            return None
        return self._load().get(product_model.strip())

    def _load(self) -> dict[str, ProductProfile]:
        if self._cache is not None:
            return self._cache
        if not self.enabled:
            self._cache = {}
            return self._cache
        f = self.feishu
        tid = self.table_id
        try:
            model_field = f.resolve_field(tid, PRODUCT_FIELDS["product_model"])
            pos_field = f.resolve_field(tid, PRODUCT_FIELDS["positioning"])
            aud_field = f.resolve_field(tid, PRODUCT_FIELDS["target_audience"])
            forbid_field = f.resolve_field(tid, PRODUCT_FIELDS["forbidden_words"])
            sp_field = f.resolve_field(tid, PRODUCT_FIELDS["selling_points"])

            out: dict[str, ProductProfile] = {}
            for record in f.list_records(tid, text_field_as_array=True):
                fields = record.get("fields", {})
                model = f.cell_text(fields.get(model_field)).strip() if model_field else ""
                if not model:
                    continue
                profile = out.setdefault(
                    model,
                    ProductProfile(
                        product_model=model,
                        positioning="",
                        target_audience="",
                        forbidden_words=[],
                        selling_points=[],
                    ),
                )
                if pos_field and not profile.positioning:
                    profile.positioning = f.cell_text(fields.get(pos_field))
                if aud_field and not profile.target_audience:
                    profile.target_audience = f.cell_text(fields.get(aud_field))
                if forbid_field:
                    profile.forbidden_words = _merge_unique(
                        profile.forbidden_words,
                        _split_terms(f.cell_text(fields.get(forbid_field))),
                    )
                if sp_field:
                    profile.selling_points = _merge_unique(
                        profile.selling_points,
                        _split_terms(f.cell_text(fields.get(sp_field))),
                    )
            logger.info("加载产品中心 {} 个产品", len(out))
            self._cache = out
        except Exception as exc:  # noqa: BLE001 - 产品中心缺失/无权限不应阻塞生产
            logger.warning("产品中心加载失败，降级为空（文案将不含产品背景）- {}", exc)
            self._cache = {}
        return self._cache


def _merge_unique(current: list[str], new_items: list[str], limit: int = 24) -> list[str]:
    out = list(current)
    for item in new_items:
        if len(out) >= limit:
            break
        if item and item not in out:
            out.append(item)
    return out


def _split_terms(text: str) -> list[str]:
    """把「禁用词/卖点」这类多值文本拆成列表（支持中英文逗号/顿号/换行/分号）。"""
    raw = str(text)
    for sep in ("，", "、", "；", ";", "\n", "\r", "|"):
        raw = raw.replace(sep, ",")
    return [t for t in (_clean_term(t) for t in raw.split(",")) if t]


def _clean_term(text: str) -> str:
    term = text.strip()
    # 产品中心里有 PH-01 这类主图编号，不是卖点，避免污染 GPT 输入。
    if term.upper().startswith("PH-") and len(term) <= 8:
        return ""
    return term


@lru_cache
def get_product_repository() -> ProductRepository:
    s = get_settings()
    table_id = s.feishu_vn_product_table_id
    if not table_id:
        return ProductRepository(None, "")
    app_token = (
        s.feishu_vn_product_app_token
        or s.feishu_vn_material_app_token
        or s.feishu_vn_bitable_app_token
    )
    return ProductRepository(make_feishu_client(app_token), table_id)
