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

from adapters.feishu import FeishuBitableClient, make_feishu_client
from adapters.publishers import Publisher, get_publisher
from app.config import get_settings
from core.feishu_fields import (
    PUBLISH_FIELD_TYPES,
    PUBLISH_FIELDS,
    RENDER_FIELD_TYPES,
    RENDER_FIELDS,
)
from services.publish.scheduler import PublishItem, plan_schedule


class PublishService:
    def __init__(
        self,
        publisher: Publisher,
        data_dir: Path,
        settings,
        render_feishu: FeishuBitableClient | None = None,
        render_table_id: str = "",
        publish_feishu: FeishuBitableClient | None = None,
        publish_table_id: str = "",
    ) -> None:
        self.publisher = publisher
        self.dir = Path(data_dir) / "publish"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.s = settings
        self.render_feishu = render_feishu
        self.render_table_id = render_table_id
        self.publish_feishu = publish_feishu
        self.publish_table_id = publish_table_id

    @property
    def publish_table_ready(self) -> bool:
        return bool(self.publish_feishu and self.publish_table_id)

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
        self._write_publish_records(items)
        self._save(on_date, items)
        logger.info("排期完成 - {} 条（{} 账号，{}）", len(items), len(accounts), on_date)
        return items

    def schedule_from_render_table(
        self,
        accounts: list[str] | None = None,
        on_date: date_cls | None = None,
        seed: int | None = None,
        mark_scheduled: bool = True,
    ) -> dict:
        """从成片表读取「已渲染(status=rendered)」的成片，按配置账号排期。

        排期后把这些成片状态置为 scheduled，避免下次重复排期。
        账号优先用传入的 accounts，否则回落到配置 PUBLISH_ACCOUNTS。
        """
        accounts = accounts or self.s.publish_account_list
        on_date = on_date or datetime.now().date()
        if not accounts:
            raise ValueError("没有发布账号（配置 PUBLISH_ACCOUNTS 或在请求里传 accounts）")
        if not (self.render_feishu and self.render_table_id):
            raise RuntimeError("未配置成片表（FEISHU_VN_RENDER_APP_TOKEN / _TABLE_ID）")

        renders = self._read_rendered()
        if not renders:
            logger.info("自动排期 - 没有 status=rendered 的成片，跳过")
            return {
                "date": str(on_date), "accounts": len(accounts),
                "candidates": 0, "scheduled": 0, "data": [],
            }

        items = self.schedule(renders, accounts, on_date, seed)
        if mark_scheduled:
            self._mark_scheduled(renders)
        return {
            "date": str(on_date),
            "accounts": len(accounts),
            "candidates": len(renders),
            "scheduled": len(items),
            "data": [it.to_dict() for it in items],
        }

    def _read_rendered(self) -> list[dict]:
        f = self.render_feishu
        tid = self.render_table_id
        status_field = f.resolve_field(tid, RENDER_FIELDS["status"])
        id_field = f.resolve_field(tid, RENDER_FIELDS["render_id"])
        link_field = f.resolve_field(tid, RENDER_FIELDS["onedrive_link"])
        title_field = f.resolve_field(tid, RENDER_FIELDS["title"])
        caption_field = f.resolve_field(tid, RENDER_FIELDS["caption"])
        records = f.list_records(tid, text_field_as_array=True)
        out: list[dict] = []
        for rec in records:
            fields = rec.get("fields", {})
            status = f.cell_text(fields.get(status_field)) if status_field else ""
            if status.strip().casefold() != "rendered":
                continue
            out.append(
                {
                    "name": f.cell_text(fields.get(id_field)) if id_field else "",
                    "onedrive_link": f.cell_link(fields.get(link_field)) if link_field else "",
                    "feishu_record_id": rec.get("record_id", ""),
                    "title": f.cell_text(fields.get(title_field)) if title_field else "",
                    "caption": f.cell_text(fields.get(caption_field)) if caption_field else "",
                }
            )
        logger.info("自动排期 - 成片表候选(status=rendered)={}", len(out))
        return out

    def _mark_scheduled(self, renders: list[dict]) -> None:
        self._mark_status(renders, "scheduled")

    def _mark_published(self, renders: list[dict]) -> None:
        self._mark_status(renders, "published")

    def _mark_status(self, renders: list[dict], status: str) -> None:
        if not (self.render_feishu and self.render_table_id):
            return
        f = self.render_feishu
        tid = self.render_table_id
        fname = f.ensure_field(tid, RENDER_FIELDS["status"], RENDER_FIELD_TYPES["status"])
        for r in renders:
            rid = r.get("feishu_record_id")
            if not rid:
                continue
            try:
                f.update_record(tid, rid, {fname: f.format_value(tid, fname, status)})
            except Exception:  # noqa: BLE001 - 单条回写失败不阻塞
                logger.exception("标记 {} 失败 - record_id={}", status, rid)

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
            self._update_publish_record(it)
            if result.ok:
                published += 1
            else:
                failed += 1
        self._save(on_date, items)
        logger.info("到点执行 - due={} published={} failed={}", due, published, failed)
        return {"date": str(on_date), "due": due, "published": published, "failed": failed}

    # ---------- 飞书发布表回写 ----------
    def _write_publish_records(self, items: list[PublishItem]) -> None:
        """排期时把每条发布计划写入飞书发布表（缺列自动创建），记录 record_id。"""
        if not self.publish_table_ready:
            return
        f = self.publish_feishu
        tid = self.publish_table_id
        for it in items:
            values = {
                "render_id": it.render_name,
                "account": it.account,
                "platform": it.platform,
                "scheduled_at": it.scheduled_at,
                "status": it.status,
                "title": it.title,
                "caption": it.caption,
                "video_url": it.video_url,
            }
            try:
                fields = self._format_publish_fields(values)
                record = f.create_record(tid, fields)
                it.publish_record_id = record.get("record_id", "")
            except Exception:  # noqa: BLE001 - 回写失败不阻塞排期
                logger.exception("发布表写入失败 - render={} account={}", it.render_name, it.account)

    def _update_publish_record(self, it: PublishItem) -> None:
        """run 后把发布状态/链接/错误回写到发布表对应记录。"""
        if not (self.publish_table_ready and it.publish_record_id):
            return
        f = self.publish_feishu
        tid = self.publish_table_id
        values = {
            "status": it.status,
            "post_url": it.post_url,
            "error": it.error,
        }
        try:
            fields = self._format_publish_fields(values)
            f.update_record(tid, it.publish_record_id, fields)
        except Exception:  # noqa: BLE001 - 单条回写失败不阻塞
            logger.exception("发布表状态回写失败 - record_id={}", it.publish_record_id)

    def _format_publish_fields(self, values: dict) -> dict:
        f = self.publish_feishu
        tid = self.publish_table_id
        fields: dict = {}
        for key, val in values.items():
            # 跳过空值：避免给 URL 等类型写空串（飞书会报 URLFieldConvFail）。
            if val is None or (isinstance(val, str) and not val.strip()):
                continue
            fname = f.ensure_field(tid, PUBLISH_FIELDS[key], PUBLISH_FIELD_TYPES[key])
            fields[fname] = f.format_value(tid, fname, val)
        return fields

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
    render_feishu = None
    if s.feishu_vn_render_app_token and s.feishu_vn_render_table_id:
        render_feishu = make_feishu_client(s.feishu_vn_render_app_token)
    publish_feishu = None
    if s.feishu_vn_publish_table_id:
        publish_app_token = (
            s.feishu_vn_publish_app_token
            or s.feishu_vn_render_app_token
            or s.feishu_vn_bitable_app_token
        )
        if publish_app_token:
            publish_feishu = make_feishu_client(publish_app_token)
    return PublishService(
        publisher=get_publisher(),
        data_dir=s.data_dir,
        settings=s,
        render_feishu=render_feishu,
        render_table_id=s.feishu_vn_render_table_id,
        publish_feishu=publish_feishu,
        publish_table_id=s.feishu_vn_publish_table_id,
    )
