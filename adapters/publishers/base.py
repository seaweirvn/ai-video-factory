"""发布器抽象（扩展点）。

- Publisher：统一接口，把一条成片发到某平台账号。
- StubPublisher：占位，只记录不真发（未配置第三方工具时用，跑通排期/回写链路）。
- ThirdPartyPublisher：通用第三方聚合发布 API（Ayrshare 风格：POST 视频 URL + 文案 + 平台）。
  具体字段随所选工具微调，配置 PUBLISH_BASE_URL/PUBLISH_API_KEY 后启用。

业务层通过 get_publisher() 拿实现，切换工具只改这里。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache

import httpx
from loguru import logger

from app.config import get_settings


@dataclass
class PublishResult:
    ok: bool
    post_id: str = ""
    post_url: str = ""
    status: str = ""          # published | scheduled | failed | recorded
    error: str = ""
    raw: dict = field(default_factory=dict)


class Publisher(ABC):
    @abstractmethod
    def publish(
        self,
        video_url: str,
        caption: str,
        account: str,
        platform: str = "tiktok",
        scheduled_at: str | None = None,
        product_keyword: str = "",
    ) -> PublishResult:
        """发布/排期一条成片。

        - scheduled_at 为 ISO 时间则交给平台定时发布。
        - product_keyword 非空时，按该关键词（一般是产品型号，如 S5）在带货商品库
          搜索并挂到视频上（TikTok 小店 / Shopee 商品）。
        """


class StubPublisher(Publisher):
    """占位发布器：不真正调用平台，只记录意图，便于先跑通排期与状态回写。"""

    def publish(
        self,
        video_url: str,
        caption: str,
        account: str,
        platform: str = "tiktok",
        scheduled_at: str | None = None,
        product_keyword: str = "",
    ) -> PublishResult:
        logger.info(
            "[stub] 记录发布 - platform={} account={} at={} product={} url={}",
            platform, account, scheduled_at, product_keyword, video_url[:60],
        )
        return PublishResult(
            ok=True,
            status="recorded",
            raw={"account": account, "platform": platform, "scheduled_at": scheduled_at},
        )


class ThirdPartyPublisher(Publisher):
    """通用第三方聚合发布 API（HTTP）。字段按所选工具在此适配。"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def publish(
        self,
        video_url: str,
        caption: str,
        account: str,
        platform: str = "tiktok",
        scheduled_at: str | None = None,
        product_keyword: str = "",
    ) -> PublishResult:
        payload: dict = {
            "post": caption,
            "platforms": [platform],
            "mediaUrls": [video_url],
            "profileKey": account,
        }
        if scheduled_at:
            payload["scheduleDate"] = scheduled_at
        try:
            resp = httpx.post(
                f"{self.base_url}/post",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return PublishResult(
                ok=True,
                post_id=str(data.get("id") or data.get("postId") or ""),
                post_url=str(data.get("postUrl") or data.get("url") or ""),
                status="scheduled" if scheduled_at else "published",
                raw=data,
            )
        except Exception as exc:
            logger.warning("第三方发布失败 - account={} err={}", account, exc)
            return PublishResult(ok=False, status="failed", error=str(exc))


def _build_bitbrowser() -> Publisher | None:
    """构造比特浏览器发布器；未配置账号映射则返回 None（由上层降级）。"""
    s = get_settings()
    account_map = s.bitbrowser_account_dict
    if not account_map:
        logger.warning("PUBLISH_PROVIDER=bitbrowser 但未配置 BITBROWSER_ACCOUNT_MAP，降级 stub")
        return None
    from adapters.publishers.bitbrowser import BitBrowserPublisher
    from adapters.storage import get_storage_client

    return BitBrowserPublisher(
        api_url=s.bitbrowser_api_url,
        account_map=account_map,
        download_fn=get_storage_client().download_share_link,
        upload_url=s.bitbrowser_upload_url,
        upload_timeout=s.bitbrowser_upload_timeout,
        action_delay_ms=(s.bitbrowser_action_delay_min_ms, s.bitbrowser_action_delay_max_ms),
        keep_open=s.bitbrowser_keep_open,
    )


@lru_cache
def get_publisher() -> Publisher:
    s = get_settings()
    want = (s.publish_provider or "auto").lower()
    configured = bool(s.publish_base_url and s.publish_api_key)
    if want == "stub":
        return StubPublisher()
    if want == "bitbrowser":
        bb = _build_bitbrowser()
        if bb is not None:
            logger.info("发布器 = bitbrowser（比特浏览器真人式发布）")
            return bb
        return StubPublisher()
    if want in ("auto", "thirdparty") and configured:
        logger.info("发布器 = thirdparty（{}）", s.publish_base_url)
        return ThirdPartyPublisher(s.publish_base_url, s.publish_api_key)
    if want == "thirdparty" and not configured:
        logger.warning("PUBLISH_PROVIDER=thirdparty 但未配置 base_url/api_key，降级 stub")
    if want == "auto" and s.bitbrowser_account_dict:
        bb = _build_bitbrowser()
        if bb is not None:
            logger.info("发布器 = bitbrowser（比特浏览器真人式发布）")
            return bb
    logger.info("发布器 = stub（未配置第三方工具，仅记录不真发）")
    return StubPublisher()
