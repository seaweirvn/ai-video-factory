"""排期引擎（纯逻辑）：把待发成片分配到账号，并在白天时段错峰排时间。

规则：每账号每天 3~5 条；发布时间落在 [start, end) 小时内，均匀分段 + 段内随机，
保证同账号内错峰、不撞点。选材/评分不在这里，这里只做"谁、什么时候、发哪条"。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta


@dataclass
class PublishItem:
    render_name: str
    account: str
    scheduled_at: str          # ISO，本地时间
    video_url: str = ""
    render_record_id: str = ""
    caption: str = ""
    title: str = ""
    platform: str = "tiktok"
    status: str = "pending"
    post_id: str = ""
    post_url: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def plan_schedule(
    renders: list[dict],
    accounts: list[str],
    on_date: date_cls,
    per_account_min: int = 3,
    per_account_max: int = 5,
    window_start_hour: int = 9,
    window_end_hour: int = 21,
    platform: str = "tiktok",
    seed: int | None = None,
) -> list[PublishItem]:
    if not renders:
        raise ValueError("没有待发布成片")
    if not accounts:
        raise ValueError("没有发布账号")

    rng = random.Random(seed)
    items: list[PublishItem] = []
    for account in accounts:
        count = rng.randint(per_account_min, per_account_max)
        count = min(count, len(renders))
        pool = renders[:]
        rng.shuffle(pool)
        chosen = pool[:count]
        times = _stagger_times(on_date, count, window_start_hour, window_end_hour, rng)
        for render, when in zip(chosen, times):
            items.append(
                PublishItem(
                    render_name=render.get("name", ""),
                    account=account,
                    scheduled_at=when.isoformat(timespec="seconds"),
                    video_url=render.get("onedrive_link", ""),
                    render_record_id=render.get("feishu_record_id", ""),
                    caption=render.get("caption", ""),
                    title=render.get("title", ""),
                    platform=platform,
                )
            )
    items.sort(key=lambda it: it.scheduled_at)
    return items


def _stagger_times(
    on_date: date_cls, count: int, start_hour: int, end_hour: int, rng: random.Random
) -> list[datetime]:
    """在 [start,end) 内均匀分 count 段，每段随机取一分钟，返回排序后的时间。"""
    start_min = start_hour * 60
    end_min = end_hour * 60
    span = max(1, end_min - start_min)
    seg = span / count
    times: list[datetime] = []
    base = datetime(on_date.year, on_date.month, on_date.day)
    for i in range(count):
        lo = int(start_min + i * seg)
        hi = int(start_min + (i + 1) * seg) - 1
        minute = rng.randint(lo, max(lo, hi))
        times.append(base + timedelta(minutes=minute))
    times.sort()
    return times
