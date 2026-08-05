"""每日自动产出 + 发布（后台任务用）。

策略：
- 每天为每个就绪窗口生成一份「日计划」：每窗 N 条（默认 5），先保证该窗每个产品至少 1 条，
  不足 N 则在该窗产品里随机补齐；超出则截断。两窗任务交错。计划落盘 data/schedule/plan_YYYYMMDD.json。
- 每天 6 个时间点（约 9/12/15/18/21/24，各 ±1h 由计划任务的随机延迟实现）触发本脚本。
  每个时段只产「本档配额」= ceil(总*(slot+1)/6) - ceil(总*slot/6)（约 2,2,1,2,2,1，合计=总数）。
  不做累计追赶：某个时段漏跑，就少产该档的量，不会在后续时段一次性补齐。
- 每条任务：产出 1 条该产品视频（Director 路径）-> 上传飞书 -> 矩阵发布到其路由窗口（TikTok+Shopee）。

幂等：计划与每条任务状态都持久化；已 done/failed 的不再重复产出。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import date, datetime
from pathlib import Path

# 允许以 `python scripts/daily_publish.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402

from app.config import get_settings  # noqa: E402
from services.pipeline import get_produce_service  # noqa: E402
from services.publish.matrix import get_matrix_publisher  # noqa: E402

BASE_POINTS = [9, 12, 15, 18, 21, 24]  # 六个时间点（24=午夜）


def current_slot_index(now: datetime, slots: int = 6) -> int:
    """把当前时间映射到最近的时间点档位（0..slots-1）。午夜后(00-02)算作 24 点档。"""
    h = now.hour + now.minute / 60.0
    if h < 8:
        h += 24
    diffs = [abs(h - b) for b in BASE_POINTS[:slots]]
    return diffs.index(min(diffs))


def build_plan(routing, per_window: int) -> list[dict]:
    """为每个就绪窗口生成任务：每产品至少 1 条，随机补齐到 per_window。"""
    tasks: list[dict] = []
    for w in routing.windows:
        if not (w.ready and w.models):
            continue
        prods = [m.strip().upper() for m in w.models if m.strip()]
        if not prods:
            continue
        chosen = list(prods)              # 每个产品至少一条
        while len(chosen) < per_window:   # 不足则随机补齐
            chosen.append(random.choice(prods))
        chosen = chosen[:per_window]      # 超出则截断（产品数 > per_window 时）
        random.shuffle(chosen)
        for p in chosen:
            tasks.append({"window": w.name, "product": p, "status": "pending"})
    random.shuffle(tasks)  # 两窗交错，避免同窗连发
    return tasks


def load_or_create_plan(plan_path: Path, routing, per_window: int, today: str) -> dict:
    if plan_path.exists():
        return json.loads(plan_path.read_text(encoding="utf-8"))
    plan = {"date": today, "per_window": per_window, "tasks": build_plan(routing, per_window)}
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("生成今日计划 - {} 条任务：{}", len(plan["tasks"]),
                ", ".join(f"{t['product']}->{t['window']}" for t in plan["tasks"]))
    return plan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-window", type=int, default=5, help="每个窗口每天产出条数")
    ap.add_argument("--slots", type=int, default=6, help="每天时间点个数")
    ap.add_argument("--dry-run", action="store_true", help="只走计划/推进，不真正产出发布")
    args = ap.parse_args()

    s = get_settings()
    sched_dir = Path(s.data_dir) / "schedule"
    sched_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    logger.add(sched_dir / f"log_{today}.log", rotation="10 MB", retention="30 days", enqueue=True)

    plan_path = sched_dir / f"plan_{today}.json"
    mp = get_matrix_publisher()
    plan = load_or_create_plan(plan_path, mp.routing, args.per_window, today)
    tasks = plan["tasks"]
    total = len(tasks)
    if total == 0:
        logger.warning("今日无任务（检查路由窗口/产品配置）")
        return

    slot = current_slot_index(datetime.now(), args.slots)
    # 本档配额（增量，不累计追赶）：漏跑的时段不补
    quota = math.ceil(total * (slot + 1) / args.slots) - math.ceil(total * slot / args.slots)
    done_this_slot = sum(
        1 for t in tasks
        if t.get("slot") == slot
        and t["status"] in ("done", "partial", "failed")
    )
    to_run = max(0, quota - done_this_slot)
    logger.info("时段 slot={}/{} 总任务={} 本档配额={} 本档已完成={} 本次执行={}",
                slot, args.slots - 1, total, quota, done_this_slot, to_run)
    if to_run == 0:
        logger.info("本时段无需执行，退出")
        return

    svc = get_produce_service()
    pending = [t for t in tasks if t["status"] == "pending"]
    ran = 0
    for t in pending:
        if ran >= to_run:
            break
        product, window = t["product"], t["window"]
        window_cfg = next((w for w in mp.routing.windows if w.name == window), None)
        voice_profile = window_cfg.voice_profile if window_cfg else ""
        t["slot"] = slot  # 记录本条在哪个时段执行（用于本档配额计数，避免同档重复超产）
        try:
            logger.info("产出开始 - {} -> {} voice={}", product, window, voice_profile or "default")
            if args.dry_run:
                t["status"] = "done"
                t["note"] = "dry_run"
                ran += 1
                continue
            res = svc.produce(
                product_model=product,
                count=1,
                voice=voice_profile or None,
                upload=True,
                generate_content=True,
            )
            renders = res.get("renders") or []
            if not renders:
                t["status"] = "failed"
                t["error"] = "; ".join(res.get("errors") or ["no_render"])
                logger.warning("产出失败 - {} err={}", product, t["error"])
            else:
                r = renders[0]
                pub = mp.publish_one(r, dry_run=False)
                t["status"] = (
                    "done" if int(pub.get("failed") or 0) == 0 else "partial"
                )
                t["render"] = r.get("name")
                t["feishu_record_id"] = r.get("feishu_record_id")
                t["published"] = pub.get("published")
                t["failed"] = pub.get("failed")
                t["targets"] = [
                    {"platform": x.get("platform"), "ok": x.get("ok"),
                     "status": x.get("status"), "video_id": x.get("video_id")}
                    for x in pub.get("targets", [])
                ]
                logger.info("完成 - {} render={} 发布 published={} failed={}",
                            product, r.get("name"), pub.get("published"), pub.get("failed"))
            ran += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻塞其它任务
            t["status"] = "failed"
            t["error"] = str(exc)
            logger.exception("任务异常 - {}", product)
            ran += 1
        finally:
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    done_total = sum(1 for t in tasks if t["status"] == "done")
    partial_total = sum(1 for t in tasks if t["status"] == "partial")
    fail_total = sum(1 for t in tasks if t["status"] == "failed")
    logger.info(
        "本时段结束 - 执行 {} 条；累计 done={} partial={} failed={} pending={}",
        ran,
        done_total,
        partial_total,
        fail_total,
        total - done_total - partial_total - fail_total,
    )


if __name__ == "__main__":
    main()
