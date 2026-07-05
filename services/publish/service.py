"""发布服务：排期落地 + 到点执行 + 状态回写。

- schedule(): 生成发布计划，落到 data/publish/<date>.json（成片表/发布表就绪后再写飞书）。
- run_due(): 找出到点未发的条目，调用发布器发布，更新状态并持久化。
成片候选可由调用方传入；后续接账号表/发布表后自动从飞书取。
"""

from __future__ import annotations

import json
from datetime import date as date_cls
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from loguru import logger

from adapters.publishers import Publisher, get_publisher
from app.config import get_settings
from services.publish.scheduler import PublishItem, plan_schedule


class PublishService:
    def __init__(self, publisher: Publisher, data_dir: Path, settings) -> None:
        self.publisher = publisher
        self.dir = Path(data_dir) / "publish"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.s = settings

    def schedule(
        self,
        renders: list[dict],
        accounts: list[str],
        on_date: date_cls | None = None,
        seed: int | None = None,
    ) -> list[PublishItem]:
        on_date = on_date or datetime.now().date()
        items = plan_schedule(
            renders,
            accounts,
            on_date,
            per_account_min=self.s.publish_per_account_min,
            per_account_max=self.s.publish_per_account_max,
            window_start_hour=self.s.publish_window_start_hour,
            window_end_hour=self.s.publish_window_end_hour,
            platform=self.s.publish_platform,
            seed=seed,
        )
        self._save(on_date, items)
        logger.info("排期完成 - {} 条（{} 账号，{}）", len(items), len(accounts), on_date)
        return items

    def run_due(self, now: datetime | None = None, on_date: date_cls | None = None) -> dict:
        now = now or datetime.now()
        on_date = on_date or now.date()
        items = self._load(on_date)
        if not items:
            return {"date": str(on_date), "due": 0, "published": 0, "failed": 0}

        published = failed = due = 0
        for it in items:
            if it.status not in ("pending",):
                continue
            if datetime.fromisoformat(it.scheduled_at) > now:
                continue
            due += 1
            result = self.publisher.publish(
                it.video_url, it.caption, it.account, it.platform, it.scheduled_at
            )
            it.status = result.status or ("published" if result.ok else "failed")
            it.post_id = result.post_id
            it.post_url = result.post_url
            it.error = result.error
            if result.ok:
                published += 1
            else:
                failed += 1
        self._save(on_date, items)
        logger.info("到点执行 - due={} published={} failed={}", due, published, failed)
        return {"date": str(on_date), "due": due, "published": published, "failed": failed}

    def list_items(self, on_date: date_cls | None = None) -> list[PublishItem]:
        return self._load(on_date or datetime.now().date())

    def _path(self, on_date: date_cls) -> Path:
        return self.dir / f"{on_date.isoformat()}.json"

    def _save(self, on_date: date_cls, items: list[PublishItem]) -> None:
        self._path(on_date).write_text(
            json.dumps([it.to_dict() for it in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self, on_date: date_cls) -> list[PublishItem]:
        path = self._path(on_date)
        if not path.exists():
            return []
        return [PublishItem(**d) for d in json.loads(path.read_text(encoding="utf-8"))]


@lru_cache
def get_publish_service() -> PublishService:
    s = get_settings()
    return PublishService(publisher=get_publisher(), data_dir=s.data_dir, settings=s)
