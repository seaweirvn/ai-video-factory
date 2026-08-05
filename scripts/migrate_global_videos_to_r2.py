"""Migrate archived VN KOL/render videos to R2 and rewrite Feishu links.

Only objects referenced by a successfully matched Feishu row are retained.
Objects uploaded by this manifest but not matched are deleted from R2.
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

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.feishu import make_feishu_client  # noqa: E402
from adapters.r2 import get_r2_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from core.feishu_fields import RENDER_FIELDS  # noqa: E402

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
KOL_ID_RE = re.compile(r"(\d{15,})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kol-source", type=Path, required=True)
    parser.add_argument("--render-source", type=Path, required=True)
    parser.add_argument("--prefix", default="video-global/03-VN")
    parser.add_argument("--phase", choices=("all", "upload", "sync"), default="all")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/r2_global_video_migration.json"),
    )
    return parser.parse_args()


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def inventory(source: Path, category: str, prefix: str, r2) -> list[dict[str, Any]]:
    if not source.is_dir():
        raise FileNotFoundError(f"视频目录不存在: {source}")
    root_key = r2.normalize_key(f"{prefix}/{category}").rstrip("/")
    rows: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in VIDEO_SUFFIXES:
            continue
        relative = path.relative_to(source).as_posix()
        key = r2.normalize_key(f"{root_key}/{relative}")
        match = KOL_ID_RE.search(path.stem) if category == "KOL-VIDEO" else None
        rows.append(
            {
                "category": category,
                "source": str(path.resolve()),
                "relative": relative,
                "key": key,
                "url": r2.public_url(key),
                "size": path.stat().st_size,
                "match_id": match.group(1) if match else path.stem.casefold(),
                "uploaded": False,
                "retained": False,
                "deleted": False,
            }
        )
    return rows


def upload_all(
    files: list[dict[str, Any]], manifest_path: Path, payload: dict[str, Any], r2
) -> None:
    old = read_manifest(manifest_path)
    old_by_key = {row.get("key"): row for row in old.get("files", [])}
    for index, row in enumerate(files, start=1):
        previous = old_by_key.get(row["key"], {})
        remote_ok = False
        if not previous.get("deleted"):
            try:
                remote_ok = (
                    int(r2.head_object(row["key"]).get("ContentLength", -1))
                    == row["size"]
                )
            except Exception:
                remote_ok = False
        if remote_ok:
            logger.info("[{}/{}] 已存在，跳过 {}", index, len(files), row["key"])
        else:
            logger.info("[{}/{}] 上传 {}", index, len(files), row["key"])
            r2.upload_file(Path(row["source"]), row["key"])
            size = int(r2.head_object(row["key"]).get("ContentLength", -1))
            if size != row["size"]:
                raise RuntimeError(
                    f"上传后大小不一致: {row['key']} local={row['size']} remote={size}"
                )
        row["uploaded"] = True
        row["deleted"] = False
        atomic_write(manifest_path, payload)


def resolve_field(client, table_id: str, candidates: list[str]) -> str:
    names = [str(field.get("field_name")) for field in client.get_fields(table_id)]
    for candidate in candidates:
        key = candidate.replace(" ", "").casefold()
        for name in names:
            if name.replace(" ", "").casefold() == key:
                return name
    raise RuntimeError(f"飞书表 {table_id} 缺少字段: {candidates}")


def cell_text(client, fields: dict[str, Any], name: str) -> str:
    return client.cell_text(fields.get(name)).strip()


def month_from_ms(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return datetime.fromtimestamp(
            int(value) / 1000, tz=timezone.utc
        ).strftime("%Y-%m")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def choose_kol(
    candidates: list[dict[str, Any]], release_month: str
) -> dict[str, Any] | None:
    if not candidates:
        return None
    if release_month:
        month_matches = [
            row
            for row in candidates
            if Path(row["relative"]).parts
            and Path(row["relative"]).parts[0] == release_month
        ]
        if month_matches:
            return month_matches[0]
    return candidates[0]


def batch_update(client, table_id: str, updates: list[dict[str, Any]]) -> None:
    for start in range(0, len(updates), 100):
        batch = updates[start : start + 100]
        client._request(
            "POST",
            f"/bitable/v1/apps/{client.app_token}/tables/{table_id}/records/batch_update",
            json={"records": batch},
        )
        logger.info(
            "飞书 {} 已更新 {}/{}",
            table_id,
            min(start + len(batch), len(updates)),
            len(updates),
        )


def sync_kol(
    files: list[dict[str, Any]], retained: set[str]
) -> dict[str, Any]:
    settings = get_settings()
    client = make_feishu_client(
        settings.feishu_vn_kol_app_token or settings.feishu_vn_bitable_app_token
    )
    table_id = settings.feishu_vn_kol_video_table_id
    id_field = resolve_field(client, table_id, ["Video ID", "Video Id"])
    link_field = resolve_field(
        client, table_id, ["ONEDRIVE LINK", "OneDrive Link", "Onedrive Link"]
    )
    release_field = resolve_field(
        client,
        table_id,
        [
            "Video release date/Ngày đăng video",
            "Video release date",
            "Ngày đăng video",
        ],
    )
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in files:
        if row["category"] == "KOL-VIDEO" and KOL_ID_RE.fullmatch(row["match_id"]):
            by_id[row["match_id"]].append(row)

    records = client.list_records(table_id, page_size=200, text_field_as_array=True)
    updates: list[dict[str, Any]] = []
    matched_records = 0
    for record in records:
        fields = record.get("fields", {})
        video_id = cell_text(client, fields, id_field)
        selected = choose_kol(
            by_id.get(video_id, []), month_from_ms(fields.get(release_field))
        )
        if not selected:
            continue
        retained.add(selected["key"])
        matched_records += 1
        current = client.cell_link(fields.get(link_field)) or client.cell_text(
            fields.get(link_field)
        )
        if current == selected["url"]:
            continue
        updates.append(
            {
                "record_id": record["record_id"],
                "fields": {
                    link_field: client.format_value(
                        table_id, link_field, selected["url"]
                    )
                },
            }
        )
    batch_update(client, table_id, updates)
    return {
        "records": len(records),
        "matched_records": matched_records,
        "updated": len(updates),
        "retained_objects": len(
            {key for key in retained if "/KOL-VIDEO/" in key}
        ),
    }


def sync_render_table(
    files: list[dict[str, Any]],
    retained: set[str],
    *,
    app_token: str,
    table_id: str,
    label: str,
) -> dict[str, Any]:
    if not app_token or not table_id:
        return {"label": label, "skipped": True}
    client = make_feishu_client(app_token)
    id_field = resolve_field(client, table_id, RENDER_FIELDS["render_id"])
    link_field = resolve_field(client, table_id, RENDER_FIELDS["onedrive_link"])
    by_stem = {
        row["match_id"]: row
        for row in files
        if row["category"] == "AI-VIDEO"
    }
    records = client.list_records(table_id, page_size=200, text_field_as_array=True)
    updates: list[dict[str, Any]] = []
    matched_records = 0
    for record in records:
        fields = record.get("fields", {})
        render_id = cell_text(client, fields, id_field).casefold()
        selected = by_stem.get(render_id)
        if not selected:
            continue
        retained.add(selected["key"])
        matched_records += 1
        current = client.cell_link(fields.get(link_field)) or client.cell_text(
            fields.get(link_field)
        )
        if current == selected["url"]:
            continue
        updates.append(
            {
                "record_id": record["record_id"],
                "fields": {
                    link_field: client.format_value(
                        table_id, link_field, selected["url"]
                    )
                },
            }
        )
    batch_update(client, table_id, updates)
    return {
        "label": label,
        "records": len(records),
        "matched_records": matched_records,
        "updated": len(updates),
    }


def sync_and_cleanup(
    files: list[dict[str, Any]], manifest_path: Path, payload: dict[str, Any], r2
) -> dict[str, Any]:
    retained: set[str] = set()
    settings = get_settings()
    kol = sync_kol(files, retained)
    vn = sync_render_table(
        files,
        retained,
        app_token=settings.feishu_vn_render_app_token,
        table_id=settings.feishu_vn_render_table_id,
        label="VN",
    )
    cn = sync_render_table(
        files,
        retained,
        app_token=settings.feishu_cn_render_app_token,
        table_id=settings.feishu_cn_render_table_id,
        label="CN",
    )

    deleted = 0
    for row in files:
        if row["key"] in retained:
            row["retained"] = True
            row["deleted"] = False
            continue
        r2.client.delete_object(Bucket=r2.bucket, Key=row["key"])
        row["retained"] = False
        row["deleted"] = True
        deleted += 1
        logger.info("删除未匹配 R2 对象 - {}", row["key"])

    summary = {
        "kol": kol,
        "renders_vn": vn,
        "renders_cn": cn,
        "uploaded_objects": len(files),
        "retained_objects": len(retained),
        "deleted_unmatched_objects": deleted,
    }
    payload["sync"] = summary
    atomic_write(manifest_path, payload)
    return summary


def main() -> None:
    args = parse_args()
    r2 = get_r2_client()
    files = inventory(args.kol_source.resolve(), "KOL-VIDEO", args.prefix, r2)
    files.extend(inventory(args.render_source.resolve(), "AI-VIDEO", args.prefix, r2))
    payload = {
        "prefix": r2.normalize_key(args.prefix),
        "bucket": r2.bucket,
        "sources": {
            "KOL-VIDEO": str(args.kol_source.resolve()),
            "AI-VIDEO": str(args.render_source.resolve()),
        },
        "files": files,
    }
    logger.info(
        "盘点完成: {} 个视频, {:.2f} GB",
        len(files),
        sum(row["size"] for row in files) / 1_000_000_000,
    )
    if args.phase in ("all", "upload"):
        upload_all(files, args.manifest, payload, r2)
        logger.info("上传阶段完成")
    if args.phase in ("all", "sync"):
        manifest = read_manifest(args.manifest)
        manifest_by_key = {
            row.get("key"): row for row in manifest.get("files", [])
        }
        if not all(
            manifest_by_key.get(row["key"], {}).get("uploaded") for row in files
        ):
            raise RuntimeError("上传清单不完整，拒绝修改飞书或删除 R2 对象")
        for row in files:
            row["uploaded"] = True
        summary = sync_and_cleanup(files, args.manifest, payload, r2)
        logger.info("迁移完成: {}", summary)


if __name__ == "__main__":
    main()
