"""Produce and publish one smoke video for VN1 and VN2."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pipeline import get_produce_service  # noqa: E402
from services.publish.matrix import get_matrix_publisher  # noqa: E402

CASES = [
    {
        "window": "VN1",
        "product": "S2",
        "voice": "vn_female_01",
    },
    {
        "window": "VN2",
        "product": "Z1",
        "voice": "vn_female_02",
    },
]


def save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def produce(manifest: Path) -> dict:
    service = get_produce_service()
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "cases": [],
    }
    for case in CASES:
        print(
            f"PRODUCE_START window={case['window']} product={case['product']} "
            f"voice={case['voice']}",
            flush=True,
        )
        result = service.produce(
            product_model=case["product"],
            count=1,
            voiceover_enabled=True,
            language="vi",
            voice=case["voice"],
            upload=True,
            generate_content=True,
        )
        item = {**case, "produce_result": result}
        if result.get("produced") == 1 and result.get("renders"):
            item["render"] = result["renders"][0]
            print(
                f"PRODUCE_OK window={case['window']} "
                f"render={item['render'].get('name')}",
                flush=True,
            )
        else:
            print(
                f"PRODUCE_FAILED window={case['window']} "
                f"errors={result.get('errors')}",
                flush=True,
            )
        payload["cases"].append(item)
        save(manifest, payload)
    return payload


def publish(manifest: Path) -> dict:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    publisher = get_matrix_publisher()
    for item in payload["cases"]:
        render = item.get("render")
        if not render:
            continue
        dry_run = publisher.publish_one(render, dry_run=True)
        item["route_check"] = dry_run
        if dry_run.get("window") != item["window"] or not dry_run.get("ready"):
            item["publish_result"] = {
                "published": 0,
                "failed": 2,
                "error": "route_check_failed",
            }
            save(manifest, payload)
            continue
        print(
            f"PUBLISH_START window={item['window']} render={render.get('name')}",
            flush=True,
        )
        item["publish_result"] = publisher.publish_one(render, dry_run=False)
        save(manifest, payload)
        print(
            "PUBLISH_RESULT="
            + json.dumps(item["publish_result"], ensure_ascii=False),
            flush=True,
        )
    payload["finished_at"] = datetime.now().isoformat(timespec="seconds")
    save(manifest, payload)
    return payload


def successful(payload: dict, *, require_publish: bool) -> bool:
    for item in payload.get("cases", []):
        if not item.get("render"):
            return False
        if require_publish:
            result = item.get("publish_result") or {}
            if result.get("published") != 2 or result.get("failed") != 0:
                return False
    return len(payload.get("cases", [])) == len(CASES)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("produce", "publish", "all"), default="all")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/manual_matrix_smoke.json"),
    )
    args = parser.parse_args()

    if args.phase in ("produce", "all"):
        payload = produce(args.manifest)
        if not successful(payload, require_publish=False):
            raise SystemExit(1)
    if args.phase in ("publish", "all"):
        payload = publish(args.manifest)
        if not successful(payload, require_publish=True):
            raise SystemExit(2)
    print(f"SMOKE_OK manifest={args.manifest}", flush=True)


if __name__ == "__main__":
    main()
