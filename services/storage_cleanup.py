"""Delete R2 videos marked by the Feishu ``Delete`` checkbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError
from loguru import logger

from adapters.feishu import make_feishu_client
from adapters.r2 import get_r2_client
from app.config import get_settings
from core.feishu_fields import MATERIAL_FIELDS, RENDER_FIELDS


@dataclass(frozen=True)
class CleanupTable:
    name: str
    app_token: str
    table_id: str
    link_candidates: list[str]


def _resolve_field(client, table_id: str, candidates: list[str]) -> str:
    fields = client.get_fields(table_id)
    names = [str(field.get("field_name") or "") for field in fields]
    for candidate in candidates:
        key = candidate.replace(" ", "").casefold()
        for name in names:
            if name.replace(" ", "").casefold() == key:
                return name
    return ""


def _is_checked(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().casefold() in {"true", "yes", "1", "checked"}


def _object_exists(r2, key: str) -> bool:
    try:
        r2.head_object(key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _table_specs(settings) -> list[CleanupTable]:
    return [
        CleanupTable(
            name="素材表",
            app_token=(
                settings.feishu_vn_material_app_token
                or settings.feishu_vn_bitable_app_token
            ),
            table_id=settings.feishu_vn_material_table_id,
            link_candidates=MATERIAL_FIELDS["onedrive_link"],
        ),
        CleanupTable(
            name="成片表",
            app_token=(
                settings.feishu_vn_render_app_token
                or settings.feishu_vn_bitable_app_token
            ),
            table_id=settings.feishu_vn_render_table_id,
            link_candidates=RENDER_FIELDS["onedrive_link"],
        ),
        CleanupTable(
            name="KOL表",
            app_token=(
                settings.feishu_vn_kol_app_token
                or settings.feishu_vn_bitable_app_token
            ),
            table_id=settings.feishu_vn_kol_video_table_id,
            link_candidates=[
                "Video Link",
                "ONEDRIVE LINK",
                "OneDrive Link",
                "ONEDRIVE_LINK",
            ],
        ),
    ]


def cleanup_deleted_videos(*, dry_run: bool = False) -> dict[str, Any]:
    """Process checked rows in all configured VN video tables.

    Only URLs under the configured R2 public domain are accepted. After a
    successful (or already completed) deletion, the stale Feishu link is
    cleared while the ``Delete`` checkbox remains checked as an audit marker.
    """

    settings = get_settings()
    r2 = get_r2_client()
    totals = {
        "checked": 0,
        "candidates": 0,
        "deleted": 0,
        "already_missing": 0,
        "links_cleared": 0,
        "skipped_no_link": 0,
        "skipped_external": 0,
        "failed": 0,
    }
    table_results: dict[str, dict[str, int]] = {}

    for spec in _table_specs(settings):
        result = {key: 0 for key in totals}
        table_results[spec.name] = result
        if not spec.app_token or not spec.table_id:
            logger.info("{} 未配置，跳过 R2 删除", spec.name)
            continue
        try:
            client = make_feishu_client(spec.app_token)
            delete_field = _resolve_field(client, spec.table_id, ["Delete"])
            link_field = _resolve_field(
                client, spec.table_id, spec.link_candidates
            )
            if not delete_field or not link_field:
                logger.warning(
                    "{} 缺少 Delete/链接列，跳过 - delete={} link={}",
                    spec.name,
                    delete_field,
                    link_field,
                )
                continue
            records = client.list_records(
                spec.table_id, page_size=200, text_field_as_array=True
            )
        except Exception:
            logger.exception("{} 读取失败", spec.name)
            result["failed"] += 1
            continue

        for record in records:
            fields = record.get("fields", {})
            if not _is_checked(fields.get(delete_field)):
                continue
            result["checked"] += 1
            cell = fields.get(link_field)
            link = client.cell_link(cell) or client.cell_text(cell)
            if not link:
                result["skipped_no_link"] += 1
                continue
            key = r2.key_from_public_url(link)
            if not key:
                result["skipped_external"] += 1
                logger.warning(
                    "{} Delete 已勾选但不是自有 R2 链接，拒绝删除 - record_id={} url={}",
                    spec.name,
                    record.get("record_id"),
                    link,
                )
                continue
            result["candidates"] += 1
            if dry_run:
                logger.info(
                    "[预演] {} 将删除 - record_id={} key={}",
                    spec.name,
                    record.get("record_id"),
                    key,
                )
                continue
            try:
                if _object_exists(r2, key):
                    r2.client.delete_object(Bucket=r2.bucket, Key=key)
                    if _object_exists(r2, key):
                        raise RuntimeError(f"R2 删除后对象仍存在: {key}")
                    result["deleted"] += 1
                else:
                    result["already_missing"] += 1
                client.update_record(
                    spec.table_id,
                    str(record.get("record_id") or ""),
                    {link_field: None},
                )
                result["links_cleared"] += 1
                logger.info(
                    "{} R2 视频已删除并清空链接 - record_id={} key={}",
                    spec.name,
                    record.get("record_id"),
                    key,
                )
            except Exception:
                result["failed"] += 1
                logger.exception(
                    "{} R2 删除失败 - record_id={} key={}",
                    spec.name,
                    record.get("record_id"),
                    key,
                )

    for result in table_results.values():
        for key in totals:
            totals[key] += result[key]
    summary = {"dry_run": dry_run, **totals, "tables": table_results}
    logger.info("飞书 Delete -> R2 清理完成 - {}", summary)
    return summary

