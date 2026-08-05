"""矩阵发布：按路由把一条成片双发到目标窗口的 TikTok + Shopee（都按型号挂商品）。

用法（一键批量）：MatrixPublisher.publish_batch(renders)
- 每条成片按型号路由到窗口（VN1/VN2/TH1…）
- 该窗口内对每个平台各发一次，product_keyword=型号（自动搜商品挂上）
- 同一窗口的多平台共用一个比特环境：本编排负责"开一次窗、发完再关一次"
- 结果可选回写飞书发布表
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx
from loguru import logger

from adapters.feishu import FeishuBitableClient
from adapters.publishers.base import PublishResult
from core.feishu_fields import (
    PUBLISH_FIELD_TYPES,
    PUBLISH_FIELDS,
    PUBLISH_RESULT_FIELD_TYPES,
    PUBLISH_RESULT_FIELDS,
)
from services.publish.routing import PublishRouting, WindowConfig, load_routing


class MatrixPublisher:
    def __init__(
        self,
        routing: PublishRouting,
        settings,
        download_fn: Callable[[str, Path], Path],
        publish_feishu: FeishuBitableClient | None = None,
        publish_table_id: str = "",
    ) -> None:
        self.routing = routing
        self.s = settings
        self.download_fn = download_fn
        self.publish_feishu = publish_feishu
        self.publish_table_id = publish_table_id

    # ---------- 对外 ----------
    def publish_batch(self, renders: list[dict], dry_run: bool = False) -> dict:
        results: list[dict] = []
        for render in renders:
            results.append(self.publish_one(render, dry_run=dry_run))
        published = sum(r.get("published", 0) for r in results)
        failed = sum(r.get("failed", 0) for r in results)
        skipped = sum(1 for r in results if r.get("skipped"))
        logger.info(
            "矩阵发布完成 - 成片 {} 条，成功 {}，失败 {}，跳过 {}",
            len(renders), published, failed, skipped,
        )
        return {
            "renders": len(renders),
            "published": published,
            "failed": failed,
            "skipped": skipped,
            "details": results,
        }

    def publish_one(
        self,
        render: dict,
        dry_run: bool = False,
        platforms: list[str] | None = None,
    ) -> dict:
        model = self.routing.extract_model(render)
        window = self.routing.resolve_by_model(model)
        base = {"render": render.get("name", ""), "model": model}
        if not window:
            logger.warning("型号 {} 未匹配任何窗口，跳过 - {}", model, base["render"])
            return {**base, "skipped": "no_route", "targets": []}

        requested = {p.strip().lower() for p in (platforms or []) if p.strip()}
        target_platforms = [
            p for p in window.platforms
            if not requested or p.lower() in requested
        ]
        if not target_platforms:
            return {
                **base,
                "window": window.name,
                "skipped": "no_requested_platform",
                "targets": [],
            }

        # dry_run：只演算路由（含未就绪窗口也展示），不真发
        if dry_run:
            targets = [
                {"platform": p, "window": window.name, "region": window.region,
                 "status": "dry_run", "ok": window.ready}
                for p in target_platforms
            ]
            return {**base, "window": window.name, "ready": window.ready,
                    "published": 0, "failed": 0, "targets": targets}

        if not window.ready:
            logger.warning(
                "窗口 {} 未就绪（enabled={} profile_id={}），跳过 - {}",
                window.name, window.enabled, bool(window.profile_id), base["render"],
            )
            return {**base, "window": window.name, "skipped": "window_not_ready", "targets": []}

        video_url = render.get("onedrive_link") or render.get("video_url") or ""
        caption = (render.get("caption") or render.get("title") or "").strip()
        if not video_url:
            return {**base, "window": window.name, "skipped": "no_video", "targets": []}

        published = failed = 0
        targets: list[dict] = []
        # 同窗只开一次：TikTok/Shopee 复用同一个 CDP 端点，避免重复开同一窗被比特限频
        ws = self._open_window(window.profile_id)
        if not ws:
            targets = [
                {"platform": p, "window": window.name, "ok": False,
                 "status": "failed", "error": "bitbrowser_open_failed"}
                for p in target_platforms
            ]
            self._writeback_render(render, window, model, caption, video_url, targets)
            return {**base, "window": window.name, "published": 0,
                    "failed": len(target_platforms), "targets": targets}
        try:
            for platform in target_platforms:
                res = self._publish_platform(platform, window, video_url, caption, model, ws)
                if self._safe_to_retry(res):
                    logger.warning(
                        "发布前置步骤失败，清理页面后重试一次 - 窗口={} 平台={} err={}",
                        window.name,
                        platform,
                        res.error if res else "unknown",
                    )
                    res = self._publish_platform(
                        platform, window, video_url, caption, model, ws
                    )
                ok = bool(res and res.ok)
                published += 1 if ok else 0
                failed += 0 if ok else 1
                targets.append({
                    "platform": platform,
                    "window": window.name,
                    "ok": ok,
                    "status": res.status if res else "failed",
                    "post_url": res.post_url if res else "",
                    "video_id": (res.post_id if res else ""),
                    "product": (res.raw.get("product") if res else None),
                    "error": res.error if res else "unsupported_platform",
                })
        finally:
            # 同窗多平台发完后统一关窗（除非配置保留）
            if not self.s.bitbrowser_keep_open:
                self._close_window(window.profile_id)
        # 合表：发布结果按平台回写到成片所在行
        self._writeback_render(render, window, model, caption, video_url, targets)
        return {**base, "window": window.name, "published": published, "failed": failed, "targets": targets}

    @staticmethod
    def _safe_to_retry(res: PublishResult | None) -> bool:
        """仅重试明确发生在点击发布前的错误，避免不确定状态下重复发帖。"""
        if not res or res.ok or res.status == "not_logged_in":
            return False
        error = (res.error or "").lower()
        markers = (
            "set_input_files",
            "选择视频文件",
            "待上传视频不存在",
            "等待元素超时",
            "file input",
        )
        return any(marker.lower() in error for marker in markers)

    # ---------- 开窗（统一管理，带重试退避） ----------
    def _open_window(self, profile_id: str) -> str:
        """打开比特窗口并返回 CDP 端点；失败（常见「请降低接口请求频率」）时关窗+退避重试。

        返回空串表示最终打不开（上层据此标记本条发布失败）。
        """
        import time as _time

        api = self.s.bitbrowser_api_url.rstrip("/")
        last = ""
        for attempt in range(5):
            try:
                resp = httpx.post(f"{api}/browser/open", json={"id": profile_id}, timeout=60.0)
                resp.raise_for_status()
                data = resp.json()
                if data.get("success", True):
                    payload = data.get("data") or {}
                    ws = payload.get("ws") or payload.get("http")
                    if ws:
                        if not ws.startswith(("ws://", "http://", "https://")):
                            ws = f"http://{ws}"
                        logger.info("比特窗口已打开 - profile={} cdp={}", profile_id, ws)
                        return ws
                    last = f"未返回调试端点: {data}"
                else:
                    last = str(data.get("msg") or data)
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
            # 限频类错误：比特要求「降低接口请求频率」，此时不要再调 close（那也是一次接口调用），
            # 只静默等更久让它冷却；其它错误（内存不足/窗口残留）才 close 清理后重试。
            low = last.lower()
            is_freq = ("频率" in last) or ("frequency" in low) or ("too" in low and "request" in low)
            if is_freq:
                wait = 15 + attempt * 10
                logger.warning("比特开窗被限频(第{}次) - {}；冷却 {}s 后重试", attempt + 1, last, wait)
            else:
                wait = 6 + attempt * 4
                logger.warning("比特开窗失败(第{}次) - {}；清理残留+关窗退避 {}s 后重试", attempt + 1, last, wait)
                try:
                    self._close_window(profile_id)
                except Exception:  # noqa: BLE001
                    pass
                # 常见根因：上次 keep_open/异常退出留下的僵尸 chromium 进程锁住该 profile 目录，
                # 导致比特无法重开（报「打开窗口失败」）。杀掉该 profile 的残留进程后再重试。
                self._kill_orphan_windows(profile_id)
            _time.sleep(wait)
        logger.warning("比特开窗最终失败 - profile={} err={}", profile_id, last)
        return ""

    @staticmethod
    def _kill_orphan_windows(profile_id: str) -> None:
        """杀掉锁住某 profile 目录的残留 BitBrowser chromium 进程（仅 Windows，尽力而为）。

        通过命令行含 BrowserCache\\<profile_id> 精确定位，绝不误伤比特主客户端进程。
        """
        import platform
        import subprocess

        if platform.system() != "Windows":
            return
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='BitBrowser.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*BrowserCache\\{profile_id}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=30, check=False,
            )
            logger.info("已清理 profile={} 的残留窗口进程", profile_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理残留窗口进程失败 - profile={} err={}", profile_id, exc)

    # ---------- 平台分发 ----------
    def _publish_platform(
        self, platform: str, window: WindowConfig, video_url: str, caption: str,
        model: str, ws_endpoint: str = "",
    ) -> PublishResult | None:
        platform = platform.lower()
        if platform == "tiktok":
            pub = self._build_tiktok(window)
        elif platform == "shopee":
            pub = self._build_shopee(window)
        else:
            logger.warning("未知平台 {}，跳过 - 窗口 {}", platform, window.name)
            return None
        logger.info(
            "矩阵发布 - 窗口={} 平台={} 型号={} 视频={}",
            window.name, platform, model, video_url[:50],
        )
        return pub.publish(
            video_url=video_url,
            caption=caption,
            account=window.name,
            platform=platform,
            product_keyword=model,
            ws_endpoint=ws_endpoint,
        )

    def _build_tiktok(self, window: WindowConfig):
        from adapters.publishers.bitbrowser import BitBrowserPublisher

        return BitBrowserPublisher(
            api_url=self.s.bitbrowser_api_url,
            account_map={window.name: window.profile_id},
            download_fn=self.download_fn,
            upload_url=self.routing.tiktok_upload_url,
            upload_timeout=self.s.bitbrowser_upload_timeout,
            action_delay_ms=(self.s.bitbrowser_action_delay_min_ms, self.s.bitbrowser_action_delay_max_ms),
            keep_open=True,  # 同窗多平台，由编排统一关窗
        )

    def _build_shopee(self, window: WindowConfig):
        from adapters.publishers.shopee import ShopeeVideoPublisher

        upload_url = self.routing.shopee_upload_url(window.region)
        return ShopeeVideoPublisher(
            api_url=self.s.bitbrowser_api_url,
            account_map={window.name: window.profile_id},
            download_fn=self.download_fn,
            upload_url=upload_url,
            upload_timeout=self.s.bitbrowser_upload_timeout,
            action_delay_ms=(self.s.bitbrowser_action_delay_min_ms, self.s.bitbrowser_action_delay_max_ms),
            keep_open=True,
        )

    def _close_window(self, profile_id: str) -> None:
        try:
            httpx.post(
                f"{self.s.bitbrowser_api_url.rstrip('/')}/browser/close",
                json={"id": profile_id},
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭比特窗口失败 - {} err={}", profile_id, exc)

    # ---------- 飞书回写（合表：写回成片所在行） ----------
    def _writeback_render(
        self,
        render: dict,
        window: WindowConfig,
        model: str,
        caption: str,
        video_url: str,
        targets: list[dict],
    ) -> None:
        if not (self.publish_feishu and self.publish_table_id):
            return
        f = self.publish_feishu
        tid = self.publish_table_id
        from datetime import datetime

        # 逐平台结果 + 挂车商品 + 发布时间
        result_values: dict = {
            "product": model,
            "published_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        errors: list[str] = []
        for t in targets:
            plat = (t.get("platform") or "").lower()
            skey, ukey = f"{plat}_status", f"{plat}_url"
            if skey in PUBLISH_RESULT_FIELDS:
                result_values[skey] = t.get("status") or ("ok" if t.get("ok") else "failed")
            if ukey in PUBLISH_RESULT_FIELDS and t.get("post_url"):
                result_values[ukey] = t.get("post_url")
            # TikTok 视频 ID 回写到「TK VIDEO ID」列
            if plat == "tiktok" and t.get("video_id"):
                result_values["tiktok_video_id"] = t["video_id"]
            if t.get("error"):
                errors.append(f"{plat}:{t['error']}")

        try:
            fields: dict = {}
            for key, val in result_values.items():
                if val is None or (isinstance(val, str) and not val.strip()):
                    continue
                fname = f.ensure_field(tid, PUBLISH_RESULT_FIELDS[key], PUBLISH_RESULT_FIELD_TYPES[key])
                fields[fname] = f.format_value(tid, fname, val)
            if errors:
                ename = f.ensure_field(tid, PUBLISH_FIELDS["error"], PUBLISH_FIELD_TYPES["error"])
                fields[ename] = f.format_value(tid, ename, " | ".join(errors))

            rid = render.get("feishu_record_id")
            if rid:
                if fields:
                    f.update_record(tid, rid, fields)
            else:
                # 没有成片行（如手动传入）时兜底新建一行，补上成片基本信息
                for key, val in {
                    "render_id": render.get("name", ""),
                    "title": render.get("title", ""),
                    "caption": caption,
                    "video_url": video_url,
                }.items():
                    if not val:
                        continue
                    fname = f.ensure_field(tid, PUBLISH_FIELDS[key], PUBLISH_FIELD_TYPES[key])
                    fields[fname] = f.format_value(tid, fname, val)
                if fields:
                    f.create_record(tid, fields)
        except Exception:  # noqa: BLE001 - 回写失败不阻塞发布
            logger.exception("矩阵发布回写飞书失败 - render={} window={}",
                             render.get("name", ""), window.name)


_matrix_singleton: MatrixPublisher | None = None


def get_matrix_publisher() -> MatrixPublisher:
    global _matrix_singleton
    if _matrix_singleton is not None:
        return _matrix_singleton
    from adapters.feishu import make_feishu_client
    from adapters.storage import get_storage_client
    from app.config import get_settings

    s = get_settings()
    routing = load_routing(s.publish_routing_file)
    publish_feishu = None
    if s.feishu_vn_publish_table_id:
        app_token = (
            s.feishu_vn_publish_app_token
            or s.feishu_vn_render_app_token
            or s.feishu_vn_bitable_app_token
        )
        if app_token:
            publish_feishu = make_feishu_client(app_token)
    _matrix_singleton = MatrixPublisher(
        routing=routing,
        settings=s,
        download_fn=get_storage_client().download_share_link,
        publish_feishu=publish_feishu,
        publish_table_id=s.feishu_vn_publish_table_id,
    )
    return _matrix_singleton
