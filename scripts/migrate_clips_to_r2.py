"""Upload local product clips to R2 and replace Feishu material links.

The upload phase is resumable: objects whose remote size matches the local file
are skipped. Feishu writes are only attempted after the complete upload has
finished and the configured material table has been validated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.feishu import make_feishu_client  # noqa: E402
from adapters.r2 import get_r2_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from core.feishu_fields import MATERIAL_FIELDS  # noqa: E402

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--prefix", default="video-clips")
    parser.add_argument(
        "--phase",
        choices=("all", "upload", "feishu"),
        default="all",
    )
    parser.add_argument("--manifest", type=Path, default=Path("data/r2_clip_migration.json"))
    parser.add_argument("--table-id", default="")
    return parser.parse_args()


def _material_id(path: Path, source: Path) -> str:
    product = path.relative_to(source).parts[0].upper()
    match = re.search(re.escape(product) + r"\d+", path.stem, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从文件名提取素材ID: {path}")
    return match.group(0).upper()


def _inventory(source: Path, prefix: str, r2) -> list[dict[str, Any]]:
    if not source.is_dir():
        raise FileNotFoundError(f"素材目录不存在: {source}")
    rows: list[dict[str, Any]] = []
    clean_prefix = r2.normalize_key(prefix).rstrip("/")
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in VIDEO_SUFFIXES:
            continue
        relative = path.relative_to(source)
        if len(relative.parts) < 2:
            raise ValueError(f"视频必须位于商品目录内: {relative}")
        product = relative.parts[0].upper()
        key = r2.normalize_key(f"{clean_prefix}/{relative.as_posix()}")
        rows.append(
            {
                "source": str(path),
                "relative": relative.as_posix(),
                "product": product,
                "material_id": _material_id(path, source),
                "key": key,
                "url": r2.public_url(key),
                "size": path.stat().st_size,
                "uploaded": False,
            }
        )
    if not rows:
        raise RuntimeError(f"素材目录没有视频: {source}")
    return rows


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _upload(rows: list[dict[str, Any]], manifest_path: Path, r2) -> None:
    old = _load_manifest(manifest_path)
    old_rows = {item.get("key"): item for item in old.get("files", [])}
    payload = {
        "source": str(Path(rows[0]["source"]).parents[len(Path(rows[0]["relative"]).parts) - 1]),
        "bucket": r2.bucket,
        "prefix": rows[0]["key"].split("/", 1)[0],
        "files": rows,
    }
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        previous = old_rows.get(row["key"], {})
        remote_ok = False
        if previous.get("uploaded") and previous.get("size") == row["size"]:
            try:
                remote_ok = int(r2.head_object(row["key"]).get("ContentLength", -1)) == row["size"]
            except Exception:
                remote_ok = False
        if not remote_ok:
            try:
                remote_ok = int(r2.head_object(row["key"]).get("ContentLength", -1)) == row["size"]
            except Exception:
                remote_ok = False
        if remote_ok:
            logger.info("[{}/{}] 已存在，跳过 {}", index, total, row["relative"])
        else:
            logger.info("[{}/{}] 上传 {}", index, total, row["relative"])
            r2.upload_file(Path(row["source"]), row["key"])
            remote_size = int(r2.head_object(row["key"]).get("ContentLength", -1))
            if remote_size != row["size"]:
                raise RuntimeError(
                    f"上传后大小不一致: {row['relative']} local={row['size']} remote={remote_size}"
                )
        row["uploaded"] = True
        _save_manifest(manifest_path, payload)


def _link_hints(cell: Any) -> set[str]:
    values: list[str] = []
    if isinstance(cell, str):
        values.append(cell)
    elif isinstance(cell, dict):
        values.extend(str(cell.get(key) or "") for key in ("text", "link"))
    elif isinstance(cell, list):
        for item in cell:
            if isinstance(item, dict):
                values.extend(str(item.get(key) or "") for key in ("text", "link"))
            else:
                values.append(str(item))
    hints: set[str] = set()
    for value in values:
        decoded = unquote(value)
        hints.add(Path(urlparse(decoded).path).name.casefold())
        hints.add(Path(decoded).name.casefold())
    return {hint for hint in hints if hint}


def _choose_candidate(candidates: list[dict[str, Any]], old_cell: Any) -> dict[str, Any] | None:
    if len(candidates) == 1:
        return candidates[0]
    hints = _link_hints(old_cell)
    matches = [
        candidate
        for candidate in candidates
        if Path(candidate["relative"]).name.casefold() in hints
    ]
    return matches[0] if len(matches) == 1 else None


def _update_feishu(rows: list[dict[str, Any]], table_id: str, manifest_path: Path) -> dict[str, Any]:
    settings = get_settings()
    app_token = settings.feishu_vn_material_app_token or settings.feishu_vn_bitable_app_token
    client = make_feishu_client(app_token)
    fields = client.get_fields(table_id)
    field_names = {str(field.get("field_name")) for field in fields}
    id_field = next((name for name in MATERIAL_FIELDS["material_id"] if name in field_names), "")
    link_field = next((name for name in MATERIAL_FIELDS["onedrive_link"] if name in field_names), "")
    if not id_field or not link_field:
        raise RuntimeError(
            f"素材表缺少必需列: material_id={id_field!r}, link={link_field!r}"
        )

    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_id[row["material_id"]].append(row)

    updates: list[dict[str, Any]] = []
    unmatched: list[str] = []
    ambiguous: list[str] = []
    unchanged = 0
    records = client.list_records(table_id, text_field_as_array=True)
    for record in records:
        record_id = str(record.get("record_id") or "")
        record_fields = record.get("fields", {})
        material_id = client.cell_text(record_fields.get(id_field)).strip().upper()
        candidates = by_id.get(material_id, [])
        if not candidates:
            unmatched.append(material_id or record_id)
            continue
        selected = _choose_candidate(candidates, record_fields.get(link_field))
        if not selected:
            ambiguous.append(material_id or record_id)
            continue
        current_url = client.cell_link(record_fields.get(link_field))
        if current_url == selected["url"]:
            unchanged += 1
            continue
        updates.append(
            {
                "record_id": record_id,
                "fields": {
                    link_field: client.format_value(table_id, link_field, selected["url"])
                },
            }
        )

    for start in range(0, len(updates), 100):
        batch = updates[start : start + 100]
        client._request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            json={"records": batch},
        )
        logger.info("飞书链接已更新 {}/{}", min(start + len(batch), len(updates)), len(updates))

    summary = {
        "table_id": table_id,
        "records": len(records),
        "updated": len(updates),
        "unchanged": unchanged,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
    }
    manifest = _load_manifest(manifest_path)
    manifest["feishu"] = summary
    _save_manifest(manifest_path, manifest)
    return summary


def main() -> None:
    args = _args()
    r2 = get_r2_client()
    rows = _inventory(args.source.resolve(), args.prefix, r2)
    logger.info(
        "素材盘点完成: {} 个视频, {} 个商品, {:.2f} GB",
        len(rows),
        len({row["product"] for row in rows}),
        sum(row["size"] for row in rows) / 1_000_000_000,
    )
    if args.phase in ("all", "upload"):
        _upload(rows, args.manifest, r2)
        logger.info("R2 上传阶段完成")
    if args.phase in ("all", "feishu"):
        if not all(row.get("uploaded") for row in _load_manifest(args.manifest).get("files", [])):
            raise RuntimeError("上传清单不完整，拒绝修改飞书链接")
        table_id = args.table_id or get_settings().feishu_vn_material_table_id
        summary = _update_feishu(rows, table_id, args.manifest)
        logger.info("飞书替换完成: {}", summary)


if __name__ == "__main__":
    main()
