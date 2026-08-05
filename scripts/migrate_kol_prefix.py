"""Move existing R2 KOL videos to a new prefix and update Feishu links."""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.feishu import make_feishu_client  # noqa: E402
from adapters.r2 import get_r2_client  # noqa: E402
from app.config import get_settings  # noqa: E402

VIDEO_ID_RE = re.compile(r"(\d{15,})")


def resolve_field(client, table_id: str, candidates: list[str]) -> str:
    names = [
        str(field.get("field_name") or "")
        for field in client.get_fields(table_id)
    ]
    for candidate in candidates:
        key = candidate.replace(" ", "").casefold()
        for name in names:
            if name.replace(" ", "").casefold() == key:
                return name
    return ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-prefix", required=True)
    parser.add_argument("--to-prefix", required=True)
    args = parser.parse_args()

    settings = get_settings()
    r2 = get_r2_client()
    old_prefix = r2.normalize_key(args.from_prefix).rstrip("/") + "/"
    new_prefix = r2.normalize_key(args.to_prefix).rstrip("/") + "/"
    objects = [
        obj
        for obj in r2.iter_objects(old_prefix)
        if int(obj.get("Size", 0)) > 0
    ]

    client = make_feishu_client(
        settings.feishu_vn_kol_app_token
        or settings.feishu_vn_bitable_app_token
    )
    table_id = settings.feishu_vn_kol_video_table_id
    id_field = resolve_field(client, table_id, ["Video ID", "Video Id"])
    link_field = resolve_field(
        client,
        table_id,
        [
            "Video Link",
            "ONEDRIVE LINK",
            "OneDrive Link",
            "ONEDRIVE_LINK",
        ],
    )
    if not id_field or not link_field:
        raise RuntimeError("KOL 表缺少 Video ID 或 Video Link 列")
    records = client.list_records(
        table_id, page_size=200, text_field_as_array=True
    )
    by_link: dict[str, list[dict]] = defaultdict(list)
    by_id: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        fields = record.get("fields", {})
        cell = fields.get(link_field)
        link = client.cell_link(cell) or client.cell_text(cell)
        if link:
            by_link[link].append(record)
        video_id = client.cell_text(fields.get(id_field)).strip()
        if video_id:
            by_id[video_id].append(record)

    copied = updated_records = deleted = unmatched = 0
    for obj in objects:
        old_key = str(obj["Key"])
        relative = old_key[len(old_prefix) :]
        new_key = new_prefix + relative
        r2.client.copy_object(
            Bucket=r2.bucket,
            CopySource={"Bucket": r2.bucket, "Key": old_key},
            Key=new_key,
            ContentType="video/mp4",
            MetadataDirective="REPLACE",
        )
        new_size = int(r2.head_object(new_key).get("ContentLength", -1))
        if new_size != int(obj.get("Size", -2)):
            raise RuntimeError(
                f"R2 复制后大小不一致: {old_key} -> {new_key}"
            )
        copied += 1

        old_url = r2.public_url(old_key)
        matched = list(by_link.get(old_url, []))
        if not matched:
            id_match = VIDEO_ID_RE.search(Path(relative).stem)
            if id_match:
                matched = list(by_id.get(id_match.group(1), []))
        if not matched:
            unmatched += 1
            print(f"UNMATCHED old={old_key} new={new_key}", flush=True)
            continue

        new_url = r2.public_url(new_key)
        for record in matched:
            client.update_record(
                table_id,
                str(record["record_id"]),
                {
                    link_field: client.format_value(
                        table_id, link_field, new_url
                    )
                },
            )
            updated_records += 1
        r2.client.delete_object(Bucket=r2.bucket, Key=old_key)
        try:
            r2.head_object(old_key)
            raise RuntimeError(f"旧对象删除后仍存在: {old_key}")
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
        deleted += 1
        print(
            f"MOVED {old_key} -> {new_key} records={len(matched)}",
            flush=True,
        )

    print(
        f"SUMMARY copied={copied} updated_records={updated_records} "
        f"deleted_old={deleted} unmatched={unmatched}",
        flush=True,
    )
    if unmatched:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
