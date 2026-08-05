"""比特浏览器（BitBrowser）真人式发布器。

思路：
- 通过比特浏览器本地 API 打开某个"环境ID"对应的窗口（独立指纹+代理，已手动登录 TikTok）。
- 用 Playwright 通过 CDP 接管这个窗口，去 TikTok 网页版模拟真人上传/发布。

要求：
- 本机运行着比特浏览器客户端（默认本地 API http://127.0.0.1:54345）。
- 目标环境已手动登录好 TikTok（本发布器只发布，不做登录）。
- pip install playwright（无需 playwright install，因为是接管已有浏览器）。

选择器集中在 _TIKTOK_SELECTORS，TikTok 网页改版时改这里即可。失败会在 tmp 里存一张截图便于排查。
"""

from __future__ import annotations

import queue
import random
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import httpx
from loguru import logger

from adapters.publishers.base import Publisher, PublishResult

# TikTok Creator Studio 上传页的关键选择器（改版时改这里）
_TIKTOK_SELECTORS = {
    "file_input": 'input[type="file"]',
    # 文案编辑区（DraftJS contenteditable）；多个候选，按顺序尝试
    "caption": [
        'div[contenteditable="true"]',
        '.public-DraftEditor-content',
        'div[data-contents="true"]',
    ],
    # 发布按钮候选
    "post_button": [
        'button[data-e2e="post_video_button"]',
        'button:has-text("Post")',
        'button:has-text("Đăng")',
        'div[data-e2e="post_video_button"] button',
    ],
}


class BitBrowserPublisher(Publisher):
    def __init__(
        self,
        api_url: str,
        account_map: dict[str, str],
        download_fn: Callable[[str, Path], Path],
        upload_url: str,
        upload_timeout: int = 300,
        action_delay_ms: tuple[int, int] = (400, 1200),
        keep_open: bool = False,
        tmp_dir: Path | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.account_map = account_map
        self.download_fn = download_fn
        self.upload_url = upload_url
        self.upload_timeout = upload_timeout
        self.action_delay_ms = action_delay_ms
        self.keep_open = keep_open
        self.tmp_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir()) / "aivf_publish"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 对外接口 ----------
    def publish(
        self,
        video_url: str,
        caption: str,
        account: str,
        platform: str = "tiktok",
        scheduled_at: str | None = None,
        product_keyword: str = "",
        ws_endpoint: str = "",
    ) -> PublishResult:
        profile_id = self.account_map.get(account)
        if not profile_id:
            msg = f"账号未配置比特环境ID: {account}（检查 BITBROWSER_ACCOUNT_MAP）"
            logger.warning(msg)
            return PublishResult(ok=False, status="failed", error=msg)

        # 上层（矩阵）已统一开窗并传入 CDP 端点时，本发布器不自开/自关窗，避免重复开同一窗被比特限频。
        external_ws = bool(ws_endpoint)
        local_path: Path | None = None
        try:
            local_path = self._resolve_local_video(video_url, account)
            if not ws_endpoint:
                ws_endpoint = self._open_browser(profile_id)
            # Playwright 同步 API 不能跑在 asyncio 事件循环线程里，放独立线程执行。
            result = self._run_in_thread(
                self._do_publish, ws_endpoint, local_path, caption, account, product_keyword
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("比特浏览器发布失败 - account={} err={}", account, exc)
            return PublishResult(ok=False, status="failed", error=str(exc))
        finally:
            if not external_ws:
                if not self.keep_open:
                    try:
                        self._close_browser(profile_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("关闭比特窗口失败 - {} err={}", profile_id, exc)
                else:
                    logger.info("按配置保留比特窗口不关闭 - profile={}", profile_id)
            # 只清理"自己下载到 temp 的临时文件"，绝不删直接传入的本地成片
            if local_path and local_path.exists() and self.tmp_dir in local_path.parents:
                try:
                    local_path.unlink()
                except OSError:
                    pass

    # ---------- 比特浏览器本地 API ----------
    def _open_browser(self, profile_id: str) -> str:
        resp = httpx.post(
            f"{self.api_url}/browser/open",
            json={"id": profile_id},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success", True):
            raise RuntimeError(f"比特 open 失败: {data.get('msg') or data}")
        payload = data.get("data") or {}
        ws = payload.get("ws") or payload.get("http")
        if not ws:
            raise RuntimeError(f"比特 open 未返回调试端点: {data}")
        # http 形态补协议前缀，供 connect_over_cdp 用
        if not ws.startswith(("ws://", "http://", "https://")):
            ws = f"http://{ws}"
        logger.info("比特窗口已打开 - profile={} cdp={}", profile_id, ws)
        return ws

    def _close_browser(self, profile_id: str) -> None:
        httpx.post(
            f"{self.api_url}/browser/close",
            json={"id": profile_id},
            timeout=30.0,
        )
        logger.info("比特窗口已关闭 - profile={}", profile_id)

    # ---------- 视频落地 ----------
    def _resolve_local_video(self, video_url: str, account: str) -> Path:
        # 本地路径直接用；否则按 OneDrive 分享链接下载。
        if video_url and not video_url.lower().startswith(("http://", "https://")):
            p = Path(video_url)
            if p.exists():
                return p
        dest = self.tmp_dir / f"{account}_{int(time.time())}.mp4"
        logger.info("下载成片到本地待发布 - {}", dest)
        return self.download_fn(video_url, dest)

    @staticmethod
    def _acquire_page(context, domains: list[str]):
        """复用窗口里已打开、疑似已登录的目标站标签页；找不到再开新页。

        返回 (page, reused)。reused=True 表示复用已有页面（结束时不应关闭它）。
        跳过 URL 含 'login' 的登录页，优先命中已登录的作品/上传页。
        """
        try:
            pages = list(context.pages)
        except Exception:  # noqa: BLE001
            pages = []
        for pg in pages:
            try:
                url = (pg.url or "").lower()
            except Exception:  # noqa: BLE001
                continue
            if not url or url.startswith("chrome"):
                continue
            if any(d in url for d in domains) and "login" not in url:
                try:
                    pg.bring_to_front()
                except Exception:  # noqa: BLE001
                    pass
                return pg, True
        return context.new_page(), False

    # ---------- Playwright 发布 ----------
    def _do_publish(
        self,
        ws_endpoint: str,
        local_path: Path,
        caption: str,
        account: str,
        product_keyword: str = "",
    ) -> PublishResult:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_endpoint)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            # 优先复用窗口里已打开且已登录的 TikTok 标签页，避免新开页落到登录墙
            page, reused = self._acquire_page(context, ["tiktok.com"])
            try:
                cur = (page.url or "").lower()
                if reused and "tiktokstudio/upload" in cur:
                    self._discard_unsaved_draft(page, account)
                page.goto(
                    self.upload_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                self._sleep()
                self._dismiss_overlays(page)
                # 导航到上传页时 TikTok 可能才显示“旧视频未保存”提示。
                # 清掉后再重载一次，确保 input 属于全新的上传会话。
                if self._discard_unsaved_draft(page, account):
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    self._sleep()
                    self._dismiss_overlays(page)

                # 落到登录页说明该环境 TikTok 未登录：立刻明确报错，不干等 60s 超时
                if "login" in (page.url or "").lower():
                    return PublishResult(
                        ok=False, status="not_logged_in",
                        error=f"TikTok 未登录（环境需先手动登录）- account={account}",
                    )

                # 提前挂上响应监听，捕获发布接口返回里的视频 ID
                captured: dict = {"video_id": ""}
                self._attach_post_id_capture(page, captured)

                self._set_tiktok_video_file(page, local_path, account)
                logger.info("已选择视频文件，等待上传处理 - {}", account)

                # 等文案编辑区出现，视为上传处理完成可编辑
                caption_box = self._wait_any(page, _TIKTOK_SELECTORS["caption"], self.upload_timeout)
                self._sleep()
                # 上传后常弹新手引导浮层，先清掉再操作，否则点击被拦截
                self._dismiss_overlays(page)
                try:
                    caption_box.click()
                except Exception:  # noqa: BLE001 - 被浮层拦截时强制点击
                    caption_box.click(force=True)
                self._sleep()
                # 清空可能的占位/默认文案后再输入
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                page.keyboard.type(caption, delay=random.randint(20, 60))
                self._sleep()
                self._dismiss_overlays(page)

                # 挂小店商品（按型号搜索选中）
                product_ok = False
                if product_keyword:
                    product_ok = self._attach_product_tiktok(page, product_keyword)
                    self._dismiss_overlays(page)

                post_btn = self._wait_any(page, _TIKTOK_SELECTORS["post_button"], 60)
                # 等按钮可点（上传/校验完成）
                for _ in range(int(self.upload_timeout / 3)):
                    if post_btn.is_enabled():
                        break
                    time.sleep(3)
                self._sleep()
                self._dismiss_overlays(page)
                # 发布前保险：若仍有残留弹窗（如挂商品未关干净），先关掉，避免污染发布
                if self._count_dialogs(page) > 0:
                    logger.warning("发布前检测到残留弹窗，先关闭 - {}", account)
                    self._close_all_dialogs(page)
                    self._sleep()
                try:
                    post_btn.click()
                except Exception:  # noqa: BLE001
                    post_btn.click(force=True)
                logger.info("已点击发布 - {}", account)

                # 点"发布"后常弹"检测中是否立即发布"确认框，需点"立即发布"
                self._confirm_post(page)

                # 成功信号：跳转到作品管理页 /tiktokstudio/content
                confirmed = self._wait_posted(page, timeout_s=40)
                if not confirmed:
                    # 检测稍慢弹窗晚出现时再兜底确认一次
                    self._confirm_post(page)
                    confirmed = self._wait_posted(page, timeout_s=20)

                post_url = self._try_capture_post_url(page)
                # 发布接口响应可能稍晚到，给几秒等它带回视频 ID
                for _ in range(10):
                    if captured["video_id"]:
                        break
                    time.sleep(1)
                video_id = captured["video_id"] or self._video_id_from_url(post_url)
                if video_id:
                    logger.info("TikTok 视频 ID 捕获 - {} => {}", account, video_id)
                else:
                    logger.warning("未捕获到 TikTok 视频 ID - {}", account)
                published = confirmed or bool(video_id)
                if not published:
                    self._discard_unsaved_draft(page, account)
                    return PublishResult(
                        ok=False,
                        status="unconfirmed",
                        error=f"TikTok 未确认发布成功 - account={account}",
                        raw={"account": account, "confirmed": False,
                             "product": product_ok, "video_id": ""},
                    )
                return PublishResult(
                    ok=published,
                    post_id=video_id,
                    status="published",
                    post_url=post_url,
                    raw={"account": account, "confirmed": confirmed,
                         "product": product_ok, "video_id": video_id},
                )
            except Exception as exc:  # noqa: BLE001
                shot = self.tmp_dir / f"error_{account}_{int(time.time())}.png"
                try:
                    page.screenshot(path=str(shot))
                    logger.warning("发布异常已截图 - {}", shot)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._discard_unsaved_draft(page, account)
                    page.goto(
                        self.upload_url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as cleanup_exc:  # noqa: BLE001
                    logger.warning(
                        "TikTok 失败后重置上传页失败 - {} err={}",
                        account,
                        cleanup_exc,
                    )
                raise
            finally:
                # 只关闭自己新开的标签页；复用的已登录页面保留不动
                if not reused:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass

    # 去越南语声调 + đ→d + 小写，绕开 Playwright 文本匹配的 Unicode 归一化坑
    _NORM_JS = (
        "const norm=s=>(s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'')"
        ".replace(/[\\u0111\\u0110]/g,'d').toLowerCase();"
    )

    def _discard_unsaved_draft(self, page, account: str) -> bool:
        """放弃上次残留的未保存编辑，避免旧 input 阻塞新文件上传。"""
        try:
            clicked = page.evaluate(
                self._NORM_JS
                + r""";(()=>{
                    const visible=e=>e && e.offsetParent!==null;
                    const markers=[
                        'chua duoc luu',
                        'tiep tuc chinh sua',
                        'not been saved',
                        'continue editing'
                    ];
                    const nodes=[...document.querySelectorAll(
                        'div,section,[role=dialog]'
                    )].filter(visible);
                    for(const node of nodes){
                        const text=norm(node.innerText);
                        if(!markers.some(k=>text.includes(k))) continue;
                        let box=node;
                        for(let i=0;i<4 && box;i++){
                            const buttons=[...box.querySelectorAll(
                                'button,[role=button]'
                            )].filter(visible);
                            const cancel=buttons.find(e=>{
                                const t=norm(e.innerText).trim();
                                return t==='huy bo'||t==='cancel'
                                    ||t.includes('discard');
                            });
                            if(cancel){ cancel.click(); return cancel.innerText; }
                            box=box.parentElement;
                        }
                    }
                    return '';
                })()"""
            )
        except Exception:  # noqa: BLE001 - 页面改版时按未命中处理
            clicked = ""
        if clicked:
            logger.info("已放弃 TikTok 旧草稿 - {} button={}", account, clicked)
            page.wait_for_timeout(1200)
            return True
        return False

    def _set_tiktok_video_file(
        self, page, local_path: Path, account: str
    ) -> None:
        """给全新上传会话选择文件；失败时重置页面并自动重试一次。"""
        local_path = Path(local_path)
        if not local_path.is_file() or local_path.stat().st_size <= 0:
            raise FileNotFoundError(f"待上传视频不存在或为空: {local_path}")

        last_exc: Exception | None = None
        for attempt in range(1, 3):
            try:
                file_input = page.locator(
                    _TIKTOK_SELECTORS["file_input"]
                ).first
                file_input.wait_for(state="attached", timeout=60000)
                file_input.set_input_files(
                    str(local_path.resolve()), timeout=120000
                )
                return
            except Exception as exc:  # noqa: BLE001 - 重置后重试一次
                last_exc = exc
                if attempt >= 2:
                    break
                logger.warning(
                    "TikTok 选择视频失败，清理旧草稿后重试 - {} err={}",
                    account,
                    exc,
                )
                self._discard_unsaved_draft(page, account)
                page.goto(
                    self.upload_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(1500)
                self._dismiss_overlays(page)
                if self._discard_unsaved_draft(page, account):
                    page.reload(
                        wait_until="domcontentloaded", timeout=60000
                    )
        raise RuntimeError(
            f"TikTok 选择视频文件重试后仍失败 - account={account}"
        ) from last_exc

    def _attach_product_tiktok(self, page, keyword: str) -> bool:
        """TikTok Studio：Thêm liên kết → Sản phẩm → 搜索型号 → 选中 → 确认。

        关键点：挂商品是两层弹窗（链接类型 / 商品列表），每层都有各自的"Tiếp/确认"
        按钮。必须**只操作最顶层弹窗**并点它的主按钮，最后**断言所有弹窗都已关闭**，
        否则残留弹窗会污染发布、导致视频不落地（语言无关，去声调匹配）。
        """
        try:
            # 1) 点"Thêm liên kết"区块里的"Thêm"，打开链接类型弹窗
            opened = page.evaluate(
                self._NORM_JS
                + r""";(()=>{
                    const all=[...document.querySelectorAll('div,span,label,h3,p')];
                    const label=all.find(e=>norm(e.innerText).includes('them lien ket') && (e.innerText||'').length<40);
                    if(!label) return false;
                    label.scrollIntoView({block:'center'});
                    let sec=label.parentElement; for(let k=0;k<4 && sec;k++){ sec=sec.parentElement; }
                    const btns=[...(sec?sec.querySelectorAll('button,[role=button]'):[])];
                    const add=btns.find(e=>{const t=norm(e.innerText);return t==='them'||t.includes('them');});
                    if(add){ add.click(); return true; }
                    return false;
                })()"""
            )
            if not opened:
                logger.warning("未找到'Thêm liên kết'入口，跳过挂商品")
                return False
            page.wait_for_timeout(2000)
            if self._count_dialogs(page) == 0:
                logger.warning("点开'Thêm'后未出现弹窗，跳过挂商品")
                return False
            # 2) 链接类型默认"Sản phẩm"（商品），点最顶层弹窗的主按钮进入商品列表
            self._select_dialog_option(page, ["san pham", "product"])  # 确保选中商品类型
            self._dialog_primary_click(page)
            page.wait_for_timeout(3000)
            # 3) 商品列表弹窗：搜索型号（打标后 Playwright fill，避开 React 输入问题）
            tagged = page.evaluate(
                self._NORM_JS
                + r""";(()=>{
                    const ds=[...document.querySelectorAll('[role=dialog]')].filter(e=>e.offsetParent!==null);
                    const d=ds[ds.length-1]; if(!d) return false;
                    const inp=[...d.querySelectorAll('input')].find(i=>i.offsetParent!==null && norm(i.placeholder).includes('tim kiem'));
                    if(inp){ inp.setAttribute('data-aivf','tkps'); return true; }
                    return false;
                })()"""
            )
            if tagged:
                box = page.locator('[data-aivf="tkps"]').first
                box.fill(keyword)
                page.wait_for_timeout(500)
                page.keyboard.press("Enter")
                page.wait_for_timeout(3500)
            # 4) 选中名称含型号的那一行（单选，限最顶层弹窗内）
            picked = page.evaluate(
                self._NORM_JS
                + r""";((kw)=>{
                    const nk=norm(kw);
                    const ds=[...document.querySelectorAll('[role=dialog]')].filter(e=>e.offsetParent!==null);
                    const d=ds[ds.length-1]; if(!d) return '';
                    const rows=[...d.querySelectorAll('tr,li,[class*=item],[class*=row]')].filter(r=>r.offsetParent!==null);
                    for(const tr of rows){
                        if(norm(tr.innerText).includes(nk)){
                            const r=tr.querySelector('input[type=radio],input[type=checkbox]');
                            if(r){ r.click(); return (tr.innerText||'').slice(0,50); }
                            const cell=tr.querySelector('td,div'); if(cell){ cell.click(); return (tr.innerText||'').slice(0,50); }
                        }
                    }
                    return '';
                })"""
                ,
                keyword,
            )
            if not picked:
                logger.warning("商品库未匹配到型号 {}，取消挂商品", keyword)
                self._close_all_dialogs(page)
                return False
            logger.info("已选中商品 - {} => {}", keyword, picked)
            page.wait_for_timeout(1000)
            # 5) 逐层点主按钮确认，直到所有弹窗关闭（最多 4 次，防止有二/三次确认）
            for _ in range(4):
                if self._count_dialogs(page) == 0:
                    break
                self._dialog_primary_click(page)
                page.wait_for_timeout(2000)
            if self._count_dialogs(page) != 0:
                logger.warning("挂商品弹窗未能关闭，商品未挂上 - 取消并放弃挂商品")
                self._close_all_dialogs(page)
                return False
            logger.info("挂商品完成，弹窗已全部关闭 - {}", keyword)
            return True
        except Exception as exc:  # noqa: BLE001 - 挂商品失败不阻塞发布
            logger.warning("TikTok 挂商品失败 - {}", exc)
            try:
                self._close_all_dialogs(page)
            except Exception:  # noqa: BLE001
                pass
            return False

    def _count_dialogs(self, page) -> int:
        """当前可见的 [role=dialog] 数量。"""
        try:
            return int(
                page.evaluate(
                    "[...document.querySelectorAll('[role=dialog]')]"
                    ".filter(e=>e.offsetParent!==null).length"
                )
            )
        except Exception:  # noqa: BLE001
            return 0

    def _dialog_primary_click(self, page) -> str:
        """只在最顶层弹窗内点"主按钮"（排除取消/返回，优先 下一步/确认/完成）。"""
        try:
            return str(
                page.evaluate(
                    self._NORM_JS
                    + r""";(()=>{
                        const ds=[...document.querySelectorAll('[role=dialog]')].filter(e=>e.offsetParent!==null);
                        const d=ds[ds.length-1]; if(!d) return 'no-dialog';
                        const btns=[...d.querySelectorAll('button,[role=button]')]
                            .filter(b=>b.offsetParent!==null && !b.disabled && b.getAttribute('aria-disabled')!=='true');
                        const cancel=['huy','cancel','quay lai','back','dong','tro ve','close'];
                        const prefer=['tiep','xac nhan','xong','hoan thanh','confirm','done','next','continue','them'];
                        const cand=btns.filter(b=>{const t=norm(b.innerText).trim(); return t && !cancel.some(c=>t===c);});
                        let t=cand.find(b=>{const x=norm(b.innerText).trim(); return prefer.some(k=>x===k);});
                        if(!t) t=cand.find(b=>{const x=norm(b.innerText).trim(); return prefer.some(k=>x.includes(k));});
                        if(!t) t=cand[cand.length-1];
                        if(t){ t.click(); return (t.innerText||'').trim(); }
                        return 'none';
                    })()"""
                )
            )
        except Exception:  # noqa: BLE001
            return "err"

    def _select_dialog_option(self, page, keys: list[str]) -> bool:
        """在最顶层弹窗内选中某个选项（radio/label，去声调匹配）。"""
        try:
            return bool(
                page.evaluate(
                    self._NORM_JS
                    + r""";((keys)=>{
                        const ds=[...document.querySelectorAll('[role=dialog]')].filter(e=>e.offsetParent!==null);
                        const d=ds[ds.length-1]; if(!d) return false;
                        const els=[...d.querySelectorAll('label,[role=radio],span,div')].filter(e=>e.offsetParent!==null);
                        for(const k of keys){
                            const t=els.find(e=>{const n=norm(e.innerText).trim(); return n===k || n.startsWith(k);});
                            if(t){ t.click(); return true; }
                        }
                        return false;
                    })""",
                    keys,
                )
            )
        except Exception:  # noqa: BLE001
            return False

    def _close_all_dialogs(self, page) -> None:
        """尽力关闭所有残留弹窗（点取消/关闭，再兜底 Esc），避免挡住发布。"""
        for _ in range(4):
            if self._count_dialogs(page) == 0:
                return
            closed = False
            try:
                closed = bool(
                    page.evaluate(
                        self._NORM_JS
                        + r""";(()=>{
                            const ds=[...document.querySelectorAll('[role=dialog]')].filter(e=>e.offsetParent!==null);
                            const d=ds[ds.length-1]; if(!d) return false;
                            const cancel=['huy','cancel','quay lai','back','dong','tro ve','close'];
                            const btns=[...d.querySelectorAll('button,[role=button]')].filter(b=>b.offsetParent!==null);
                            let t=btns.find(b=>{const x=norm(b.innerText).trim(); return cancel.some(c=>x===c);});
                            if(!t) t=[...d.querySelectorAll('[aria-label]')].find(e=>/close|dong|huy/i.test(e.getAttribute('aria-label')||''));
                            if(t){ t.click(); return true; }
                            return false;
                        })()"""
                    )
                )
            except Exception:  # noqa: BLE001
                closed = False
            if not closed:
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(800)

    def _click_by_norm(self, page, keys: list[str]) -> bool:
        """按去声调后的按钮文字点击（keys 为已归一化的候选，如 'tiep'）。"""
        try:
            return bool(
                page.evaluate(
                    self._NORM_JS
                    + r""";((keys)=>{
                        const btns=[...document.querySelectorAll('button,[role=button]')].filter(e=>e.offsetParent!==null);
                        for(const k of keys){
                            const t=btns.find(e=>{const n=norm(e.innerText).trim(); return n===k || n.includes(k);});
                            if(t){ t.click(); return true; }
                        }
                        return false;
                    })"""
                    ,
                    keys,
                )
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _confirm_post(page) -> bool:
        """处理"Tiếp tục đăng?（视频检测中，是否立即发布）"确认弹窗。

        越南语按钮文字（Đăng ngay）受 Unicode 归一化影响，Playwright 文本匹配常失效，
        改用浏览器内 JS 按 ASCII 子串匹配点击（"ngay" / "post now" / "anyway"）。
        """
        page.wait_for_timeout(1500)
        try:
            clicked = page.evaluate(
                """() => {
                    const kw = ['ngay', 'post now', 'anyway'];
                    const els = [...document.querySelectorAll('button,[role=\"button\"]')]
                        .filter(e => e.offsetParent !== null);
                    for (const e of els) {
                        const t = (e.innerText || '').toLowerCase().trim();
                        if (kw.some(k => t.includes(k))) { e.click(); return t; }
                    }
                    return '';
                }"""
            )
        except Exception:  # noqa: BLE001
            clicked = ""
        if clicked:
            logger.info("发布确认弹窗已处理 - {}", clicked)
            return True
        return False

    @staticmethod
    def _wait_posted(page, timeout_s: int) -> bool:
        try:
            page.wait_for_url("**/tiktokstudio/content**", timeout=timeout_s * 1000)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _dismiss_overlays(page) -> None:
        """关掉 TikTok Studio 的新手引导（react-joyride）等浮层，避免拦截点击。"""
        skip_selectors = [
            'button[data-test-id="button-skip"]',
            'button[aria-label="Skip"]',
            'button[data-action="skip"]',
            'button:has-text("Skip")',
            'button:has-text("Got it")',
            'button:has-text("Bỏ qua")',
            'button:has-text("Đã hiểu")',
            '.react-joyride__tooltip button',
        ]
        for sel in skip_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=2000)
                    page.wait_for_timeout(400)
            except Exception:  # noqa: BLE001 - 尽力关闭，失败忽略
                pass
        # 兜底：直接移除残留的引导浮层节点
        try:
            page.evaluate(
                "document.querySelectorAll("
                "'#react-joyride-portal, .react-joyride__overlay, .react-joyride__spotlight'"
                ").forEach(function(e){e.remove();});"
            )
        except Exception:  # noqa: BLE001
            pass

    def _wait_any(self, page, selectors: list[str], timeout_s: int):
        """按候选选择器顺序等待，返回第一个可见的 locator。"""
        deadline = time.time() + timeout_s
        last_exc: Exception | None = None
        while time.time() < deadline:
            for sel in selectors:
                loc = page.locator(sel).first
                try:
                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
            time.sleep(1)
        raise RuntimeError(f"等待元素超时: {selectors} ({last_exc})")

    @staticmethod
    def _try_capture_post_url(page) -> str:
        try:
            url = page.url
            if "/video/" in url or "tiktok.com/@" in url:
                return url
        except Exception:  # noqa: BLE001
            pass
        return ""

    # TikTok 视频 ID：约 15~21 位纯数字（雪花 ID）
    _VIDEO_ID_RE = re.compile(r"^\d{15,21}$")
    _VIDEO_ID_KEYS = (
        "item_id", "itemid", "item_id_str", "aweme_id", "awemeid",
        "video_id", "videoid", "post_id", "postid",
    )

    @classmethod
    def _video_id_from_url(cls, url: str) -> str:
        m = re.search(r"/video/(\d{15,21})", url or "")
        return m.group(1) if m else ""

    @classmethod
    def _attach_post_id_capture(cls, page, sink: dict) -> None:
        """监听发布相关接口响应，深挖 JSON 里形如 item_id 的视频 ID 存入 sink。"""

        def _walk(obj) -> None:
            if sink["video_id"]:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if (
                        isinstance(v, (str, int))
                        and str(k).lower() in cls._VIDEO_ID_KEYS
                        and cls._VIDEO_ID_RE.match(str(v))
                    ):
                        sink["video_id"] = str(v)
                        return
                    _walk(v)
            elif isinstance(obj, list):
                for it in obj:
                    _walk(it)

        def _on_response(resp) -> None:
            try:
                if sink["video_id"]:
                    return
                u = (resp.url or "").lower()
                if not any(t in u for t in ("post", "publish", "project", "aweme", "item")):
                    return
                ct = (resp.headers or {}).get("content-type", "")
                if "json" not in ct.lower():
                    return
                _walk(resp.json())
            except Exception:  # noqa: BLE001 - 抓 ID 失败不影响发布
                pass

        try:
            page.on("response", _on_response)
        except Exception:  # noqa: BLE001
            pass

    def _sleep(self) -> None:
        lo, hi = self.action_delay_ms
        time.sleep(random.randint(lo, hi) / 1000.0)

    @staticmethod
    def _run_in_thread(fn, *args) -> PublishResult:
        result: "queue.Queue" = queue.Queue(maxsize=1)

        def _worker() -> None:
            try:
                result.put((True, fn(*args)))
            except Exception as exc:  # noqa: BLE001
                result.put((False, exc))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join()
        ok, payload = result.get()
        if ok:
            return payload
        raise payload
