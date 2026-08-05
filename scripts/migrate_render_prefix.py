"""Move existing R2 renders to a new prefix and update Feishu links."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.feishu import make_feishu_client  # noqa: E402
from adapters.r2 import get_r2_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from core.feishu_fields import RENDER_FIELDS  # noqa: E402


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
        settings.feishu_vn_render_app_token
        or settings.feishu_vn_bitable_app_token
    )
    table_id = settings.feishu_vn_render_table_id
    id_field = client.resolve_field(table_id, RENDER_FIELDS["render_id"])
    link_field = client.resolve_field(table_id, RENDER_FIELDS["onedrive_link"])
    if not id_field or not link_field:
        raise RuntimeError("越南成片表缺少成片ID或成片链接列")
    records = client.list_records(
        table_id, page_size=200, text_field_as_array=True
    )
    by_id = {
        client.cell_text(record.get("fields", {}).get(id_field)).strip().casefold(): record
        for record in records
        if client.cell_text(record.get("fields", {}).get(id_field)).strip()
    }

    copied = updated = deleted = unmatched = 0
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

        render_id = Path(relative).stem.casefold()
        record = by_id.get(render_id)
        if not record:
            unmatched += 1
            print(f"UNMATCHED old={old_key} new={new_key}", flush=True)
            continue
        new_url = r2.public_url(new_key)
        client.update_record(
            table_id,
            str(record["record_id"]),
            {
                link_field: client.format_value(
                    table_id, link_field, new_url
                )
            },
        )
        updated += 1
        r2.client.delete_object(Bucket=r2.bucket, Key=old_key)
        try:
            r2.head_object(old_key)
            raise RuntimeError(f"旧对象删除后仍存在: {old_key}")
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
        deleted += 1
        print(f"MOVED {old_key} -> {new_key}", flush=True)

    print(
        f"SUMMARY copied={copied} updated={updated} "
        f"deleted_old={deleted} unmatched={unmatched}",
        flush=True,
    )
    if unmatched:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
