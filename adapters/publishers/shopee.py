"""Shopee Video 真人式发布器（比特浏览器 + Playwright）。

Shopee 卖家中心创作者视频上传页（banhang.shopee.vn/creator-center/video-upload/upload）：
- 选视频 → 填 Caption → Add Product（按型号搜索选中，最多 6 个）→ Post Now → Post。

与 TikTok 复用同一个比特环境（同一窗口里既登录了 TikTok 也登录了 Shopee）。
页面为英文，按钮文本匹配简单；商品名为越南语，按型号子串匹配所在行。
"""

from __future__ import annotations

import queue
import random
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import httpx
from loguru import logger

from adapters.publishers.base import Publisher, PublishResult

_SHOPEE_UPLOAD_URL = "https://banhang.shopee.vn/creator-center/video-upload/upload"

# 选品弹窗内按型号勾选商品行（弹窗为越南语表格：Sản phẩm/Giá/Tồn kho）。
# 匹配规则：整词边界匹配型号（如 Z2 不误匹配 Z25），或「seaweir 型号」子串。
_PICK_ROW_JS = r"""((kw) => {
    const m = (kw || '').toLowerCase();
    const dlg = [...document.querySelectorAll('[role=dialog],.eds-modal__box,.eds-react-modal__box,.eds-modal')]
        .filter(e => e.offsetParent !== null).pop();
    if (!dlg) return '';
    const re = new RegExp('(^|[^a-z0-9])' + m.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '([^a-z0-9]|$)', 'i');
    const rows = [...dlg.querySelectorAll('tr,[class*=item],[class*=row]')].filter(r => r.offsetParent !== null);
    for (const tr of rows) {
        const t = (tr.innerText || '').toLowerCase();
        if (!t) continue;
        if (t.includes('seaweir ' + m) || re.test(t)) {
            const c = tr.querySelector('input[type=checkbox],input[type=radio]');
            if (c) { c.click(); return (tr.innerText || '').replace(/\s+/g, ' ').slice(0, 60); }
        }
    }
    return '';
})"""


class ShopeeVideoPublisher(Publisher):
    def __init__(
        self,
        api_url: str,
        account_map: dict[str, str],
        download_fn: Callable[[str, Path], Path],
        upload_url: str = _SHOPEE_UPLOAD_URL,
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

    def publish(
        self,
        video_url: str,
        caption: str,
        account: str,
        platform: str = "shopee",
        scheduled_at: str | None = None,
        product_keyword: str = "",
        ws_endpoint: str = "",
    ) -> PublishResult:
        profile_id = self.account_map.get(account)
        if not profile_id:
            msg = f"账号未配置比特环境ID: {account}"
            logger.warning(msg)
            return PublishResult(ok=False, status="failed", error=msg)

        # 上层（矩阵）已统一开窗并传入 CDP 端点时，本发布器不自开/自关窗，避免重复开同一窗被比特限频。
        external_ws = bool(ws_endpoint)
        local_path: Path | None = None
        try:
            local_path = self._resolve_local_video(video_url, account)
            ws = ws_endpoint or self._open_browser(profile_id)
            return self._run_in_thread(
                self._do_publish, ws, local_path, caption, account, product_keyword
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shopee 发布失败 - account={} err={}", account, exc)
            return PublishResult(ok=False, status="failed", error=str(exc))
        finally:
            if not external_ws:
                if not self.keep_open:
                    try:
                        self._close_browser(profile_id)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    logger.info("按配置保留比特窗口不关闭 - profile={}", profile_id)
            # 只清理"自己下载到 temp 的临时文件"，绝不删直接传入的本地成片
            if local_path and local_path.exists() and self.tmp_dir in local_path.parents:
                try:
                    local_path.unlink()
                except OSError:
                    pass

    # ---------- 比特本地 API ----------
    def _open_browser(self, profile_id: str) -> str:
        resp = httpx.post(f"{self.api_url}/browser/open", json={"id": profile_id}, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        ws = (data.get("data") or {}).get("ws") or (data.get("data") or {}).get("http")
        if not ws:
            raise RuntimeError(f"比特 open 未返回调试端点: {data}")
        if not ws.startswith(("ws://", "http://", "https://")):
            ws = f"http://{ws}"
        logger.info("比特窗口已打开(Shopee) - profile={} cdp={}", profile_id, ws)
        return ws

    def _close_browser(self, profile_id: str) -> None:
        httpx.post(f"{self.api_url}/browser/close", json={"id": profile_id}, timeout=30.0)

    def _resolve_local_video(self, video_url: str, account: str) -> Path:
        if video_url and not video_url.lower().startswith(("http://", "https://")):
            p = Path(video_url)
            if p.exists():
                return p
        dest = self.tmp_dir / f"shopee_{account}_{int(time.time())}.mp4"
        logger.info("下载成片到本地待发布(Shopee) - {}", dest)
        return self.download_fn(video_url, dest)

    # ---------- Playwright 发布 ----------
    def _do_publish(
        self, ws: str, local_path: Path, caption: str, account: str, product_keyword: str
    ) -> PublishResult:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            # 优先复用已打开且已登录的 Shopee 卖家中心标签页
            page, reused = self._acquire_page(ctx, ["shopee."])
            try:
                cur = (page.url or "").lower()
                if not (reused and "video-upload/upload" in cur):
                    page.goto(self.upload_url, wait_until="load", timeout=60000)
                self._sleep()
                self._set_video_file(page, local_path)
                logger.info("已选择视频文件(Shopee)，等待上传处理 - {}", account)

                # 等视频上传+处理完成、编辑表单就绪（大视频处理可能数分钟）：
                # 上传/处理中页面只显示进度，不渲染 Caption/Add Product/Post 表单。
                # 必须等「Add Product」入口出现再继续，否则会漏挂车甚至空发。
                ready = self._wait_editor_ready(page, max(self.upload_timeout, 600))
                if not ready:
                    logger.warning("Shopee 视频处理超时、编辑器未就绪（仍尝试后续步骤）- {}", account)
                self._sleep()

                # 填 Caption（contenteditable）
                self._fill_caption(page, caption)
                self._sleep()

                # 挂商品
                product_ok = False
                if product_keyword:
                    product_ok = self._attach_product(page, product_keyword)
                    self._sleep()

                # 勾选"同意条款"复选框（不勾则点 Post 无效，Shopee 会提示需先同意）
                self._agree_tos(page)
                self._sleep()

                # 「Đăng ngay」(立即发布) 默认选中；点「Đăng」(发布)，排除立即发布/定时/存草稿
                self._click_text(
                    page, ["Đăng", "Post"],
                    exclude=["Đăng ngay", "Lên lịch đăng bài sau", "Lưu bản nháp",
                             "Post Now", "Post Later", "Schedule", "Save Draft"],
                )
                logger.info("已点击发布(Shopee) - {}", account)

                confirmed = self._wait_success(page, 40)
                # 有的会弹二次确认
                if not confirmed:
                    self._agree_tos(page)
                    self._click_text(
                        page, ["Xác nhận", "Confirm", "OK", "Đăng", "Post"],
                        exclude=["Đăng ngay", "Post Now"],
                    )
                    confirmed = self._wait_success(page, 20)

                return PublishResult(
                    ok=True,
                    status="published" if confirmed else "recorded",
                    raw={"account": account, "confirmed": confirmed, "product": product_ok},
                )
            except Exception:  # noqa: BLE001
                shot = self.tmp_dir / f"shopee_error_{account}_{int(time.time())}.png"
                try:
                    page.screenshot(path=str(shot))
                    logger.warning("Shopee 发布异常已截图 - {}", shot)
                except Exception:  # noqa: BLE001
                    pass
                raise
            finally:
                # 只关闭自己新开的标签页；复用的已登录页面保留不动
                if not reused and not self.keep_open:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass

    @staticmethod
    def _acquire_page(context, domains: list[str]):
        """复用窗口里已打开、疑似已登录的目标站标签页；找不到再开新页。

        返回 (page, reused)。跳过 URL 含 'login' 的登录页。
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

    def _set_video_file(self, page, local_path: Path) -> None:
        """选择视频文件。实测直接喂隐藏 input 最稳（越南语站点亦然）；失败再走 file_chooser。"""
        page.wait_for_selector('input[type="file"]', state="attached", timeout=60000)
        try:
            page.locator('input[type="file"]').first.set_input_files(
                str(local_path), timeout=60000
            )
            return
        except Exception as exc:  # noqa: BLE001 - 回退到 file_chooser
            logger.warning("Shopee set_input_files 失败，改用 file_chooser - {}", exc)
        with page.expect_file_chooser(timeout=15000) as fc:
            if not self._click_any(page, ["chọn video", "tải video", "select videos", "select video"]):
                page.locator('input[type="file"]').first.click(timeout=5000)
        fc.value.set_files(str(local_path))

    def _fill_caption(self, page, caption: str) -> None:
        try:
            box = page.locator('div[contenteditable="true"]').first
            box.wait_for(state="visible", timeout=30000)
            box.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            page.keyboard.type(caption, delay=random.randint(20, 55))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shopee 文案填写失败（继续）- {}", exc)

    def _attach_product(self, page, keyword: str) -> bool:
        """挂商品（越南语界面）：点「Thêm sản phẩm」→ 搜索型号 →「Áp dụng」过滤 → 勾选行 →「Xác nhận」。

        英文站点保留兜底文案（Add product / Apply / Confirm）。
        """
        try:
            # 打开选品弹窗：编辑器里的「Thêm sản phẩm (x/6)」入口（span/div，非 button）
            opened = False
            for t in ["Thêm sản phẩm", "Add product", "Add products", "Add Product"]:
                try:
                    loc = page.get_by_text(t, exact=False)
                    if loc.count():
                        loc.last.click(timeout=5000)
                        opened = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not opened:
                logger.warning("Shopee 未找到挂商品入口（Thêm sản phẩm）")
                return False
            if not self._wait_modal_search(page, 20):
                logger.warning("Shopee 选品弹窗未就绪")
                return False
            # 搜索型号并应用过滤（越南语站需点「Áp dụng」才生效）
            self._search_product(page, keyword)
            # 勾选名称匹配型号的行（弹窗内）
            picked = page.evaluate(_PICK_ROW_JS, keyword)
            if not picked:
                logger.warning("Shopee 商品库未匹配到型号 {}", keyword)
                self._click_text(page, ["Hủy", "Cancel"])
                return False
            logger.info("Shopee 已选中商品 - {} => {}", keyword, picked)
            page.wait_for_timeout(600)
            self._click_text(page, ["Xác nhận", "Confirm"], exclude=["Hủy", "Cancel"])
            page.wait_for_timeout(1500)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shopee 挂商品失败（继续发布）- {}", exc)
            return False

    @staticmethod
    def _wait_modal_search(page, timeout_s: int) -> bool:
        """等选品弹窗内的搜索框出现，作为弹窗就绪信号。"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                ok = page.evaluate(
                    r"""(() => {
                        const dlg = [...document.querySelectorAll('[role=dialog],.eds-modal__box,.eds-react-modal__box,.eds-modal')]
                            .filter(e => e.offsetParent !== null).pop();
                        if (!dlg) return false;
                        return !!dlg.querySelector('input');
                    })"""
                )
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                return True
            time.sleep(1)
        return False

    def _search_product(self, page, keyword: str) -> None:
        """在选品弹窗搜索框输入型号并应用过滤；失败则回退全表扫描。"""
        try:
            box = page.locator(
                '[role=dialog] input[placeholder*="Tìm kiếm"], '
                '[role=dialog] input[placeholder*="Search"], '
                '.eds-modal__box input[type="text"], '
                '.eds-react-modal__box input[type="text"]'
            ).first
            box.fill(keyword)
            page.wait_for_timeout(400)
            # 越南语界面搜索需点「Áp dụng」(应用) 才过滤；英文站兜底 Apply/回车
            if not self._click_text(page, ["Áp dụng", "Apply"]):
                page.keyboard.press("Enter")
            page.wait_for_timeout(2500)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shopee 商品搜索失败（改用全表扫描）- {}", exc)

    @staticmethod
    def _agree_tos(page) -> bool:
        """勾选发布前的同意条款复选框。

        越南语：「Khi đăng bài, bạn chấp nhận với Điều khoản dịch vụ Shopee Video」
        英文：「By posting, you agree to ... Terms of Service」
        仅点击与条款文案相邻的复选框（向上就近查找），避免误勾其它选项（如「允许二次创作」）。
        找不到条款复选框则不动任何勾选项直接返回（部分界面发布时自动同意）。
        """
        try:
            ok = page.evaluate(
                r"""(() => {
                    const vis = e => e && e.offsetParent !== null;
                    // 定位含条款文案的那一行（越南语/英文）
                    const row = [...document.querySelectorAll('*')].find(e => {
                        if (!vis(e)) return false;
                        const t = (e.innerText || '').toLowerCase();
                        if (t.length >= 200) return false;
                        return t.includes('điều khoản dịch vụ')
                            || t.includes('khi đăng bài')
                            || (t.includes('terms of service') && (t.includes('agree') || t.includes('posting')));
                    });
                    if (!row) return false;
                    // 从该行就近向上查找相邻复选框（最多 4 层），避免抓到无关勾选项
                    let scope = row;
                    for (let i = 0; i < 4 && scope; i++) {
                        const cb = scope.querySelector && scope.querySelector('input[type=checkbox]');
                        if (cb) {
                            if (!cb.checked) {
                                const wrap = cb.closest('label,.eds-checkbox,[class*=checkbox]') || cb;
                                wrap.scrollIntoView({ block: 'center' });
                                wrap.click();
                                if (!cb.checked) cb.click();
                            }
                            return !!cb.checked;
                        }
                        scope = scope.parentElement;
                    }
                    return false;
                })"""
            )
            if ok:
                logger.info("Shopee 已勾选同意条款")
            else:
                logger.info("Shopee 未见条款复选框（可能发布时自动同意）")
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shopee 勾选同意条款异常（继续）- {}", exc)
            return False

    # ---------- 通用工具 ----------
    @staticmethod
    def _click_any(page, texts_lower: list[str]) -> bool:
        """扫所有元素（含 span/div，非仅 button）点最内层匹配文本的可点元素。"""
        try:
            return bool(
                page.evaluate(
                    r"""((texts) => {
                        let cands=[...document.querySelectorAll('button,[role=button],a,span,div')]
                            .filter(e=>e.offsetParent!==null);
                        cands=cands.filter(e=>{const t=(e.innerText||'').trim().toLowerCase(); return t && t.length<40 && texts.some(x=>t.includes(x));});
                        cands.sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length);
                        if(cands[0]){ cands[0].scrollIntoView({block:'center'}); cands[0].click(); return true; }
                        return false;
                    })""",
                    texts_lower,
                )
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _click_text(page, texts: list[str], exclude: list[str] | None = None) -> bool:
        exclude = exclude or []
        try:
            return bool(
                page.evaluate(
                    r"""(([texts, exclude]) => {
                        const btns=[...document.querySelectorAll('button,[role=button]')].filter(e=>e.offsetParent!==null);
                        const bad=t=>exclude.some(x=>t.toLowerCase()===x.toLowerCase() || t.toLowerCase().includes(x.toLowerCase()));
                        for(const want of texts){
                            const t=btns.find(e=>{const s=(e.innerText||'').trim(); return (s.toLowerCase()===want.toLowerCase() || s.toLowerCase().includes(want.toLowerCase())) && !bad(s);});
                            if(t){ t.click(); return true; }
                        }
                        return false;
                    })""",
                    [texts, exclude],
                )
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _wait_text(page, texts: list[str], timeout_s: int) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            hit = page.evaluate(
                r"""((texts) => {
                    const body=(document.body.innerText||'').toLowerCase();
                    return texts.some(t=>body.includes(t.toLowerCase()));
                })""",
                texts,
            )
            if hit:
                return True
            time.sleep(1.5)
        return False

    @staticmethod
    def _wait_editor_ready(page, timeout_s: int) -> bool:
        """等视频上传+处理完成、编辑表单就绪，返回是否就绪。

        上传/处理中页面只显示进度条，不渲染编辑表单；大视频处理可能数分钟。
        就绪判定：出现「Add Product」挂车入口（首选）；或在无「上传/处理/进度%」
        字样的前提下出现「Add Caption / Post Now」，兜底覆盖界面文案差异。
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                ready = page.evaluate(
                    r"""(() => {
                        const body = (document.body.innerText || '').toLowerCase();
                        // 越南语站：上传成功 / 编辑器出现「添加商品 + 发布」
                        if (body.includes('tải thành công')) return true;
                        if (body.includes('thêm sản phẩm') && body.includes('đăng ngay')) return true;
                        // 英文站兜底
                        if (body.includes('add product') || body.includes('add products')) return true;
                        return false;
                    })"""
                )
            except Exception:  # noqa: BLE001
                ready = False
            if ready:
                return True
            time.sleep(2)
        return False

    @staticmethod
    def _wait_success(page, timeout_s: int) -> bool:
        """可靠信号：发布成功后页面跳转到已发布管理页 /video-upload/manage。

        注意：页面右下角有常驻的反馈组件含 "Submitted Successfully!" 字样，
        不能用它做成功判定（会误报），只认 URL 跳转。
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if "/video-upload/manage" in page.url:
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
        return False

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
