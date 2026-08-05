"""发布路由：产品型号 → 窗口（比特环境），每个窗口含 TK + Shopee 双平台。

一个"窗口"= 一个比特浏览器环境，里面同时登录了某地区某账号的 TikTok 与 Shopee。
配置在 data/publish_routing.json，加窗口/地区只改配置，代码不动（扩展点）。

- 窗口1 = VN1（越南1）
- 窗口2 = VN2（越南2）
- 窗口11 = TH1（泰国1）
- 以后加窗口（如 win12=TH2、win21=ID1…）继续往 windows 里加即可。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

_DEFAULT_TIKTOK_UPLOAD = "https://www.tiktok.com/tiktokstudio/upload"
_DEFAULT_SHOPEE_UPLOAD = {
    "VN": "https://banhang.shopee.vn/creator-center/video-upload/upload",
    "TH": "https://seller.shopee.co.th/creator-center/video-upload/upload",
}


@dataclass
class WindowConfig:
    id: str
    name: str
    region: str
    profile_id: str
    voice_profile: str = ""
    platforms: list[str] = field(default_factory=lambda: ["tiktok", "shopee"])
    models: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def ready(self) -> bool:
        """窗口可用：启用 + 已填比特环境ID。"""
        return bool(self.enabled and self.profile_id)


class PublishRouting:
    def __init__(
        self,
        windows: list[WindowConfig],
        regions: dict[str, dict] | None = None,
        tiktok_upload_url: str = _DEFAULT_TIKTOK_UPLOAD,
    ) -> None:
        self.windows = windows
        self.regions = regions or {}
        self.tiktok_upload_url = tiktok_upload_url or _DEFAULT_TIKTOK_UPLOAD
        # 型号 -> 窗口 索引（大写归一）
        self._model_index: dict[str, WindowConfig] = {}
        for w in windows:
            for m in w.models:
                key = m.strip().upper()
                if not key:
                    continue
                if key in self._model_index:
                    logger.warning(
                        "路由冲突：型号 {} 同时映射到 {} 和 {}，以后者为准",
                        key, self._model_index[key].name, w.name,
                    )
                self._model_index[key] = w

    # ---------- 查询 ----------
    def resolve_by_model(self, model: str) -> WindowConfig | None:
        return self._model_index.get((model or "").strip().upper())

    def window_by_id(self, window_id: str) -> WindowConfig | None:
        return next((w for w in self.windows if w.id == window_id), None)

    def shopee_upload_url(self, region: str) -> str:
        region = (region or "").upper()
        cfg = self.regions.get(region) or {}
        return cfg.get("shopee_upload_url") or _DEFAULT_SHOPEE_UPLOAD.get(region, "")

    @staticmethod
    def extract_model(render: dict) -> str:
        """从成片信息里取产品型号：优先 product_model，其次名字前缀（S5_xxx → S5）。"""
        m = (render.get("product_model") or "").strip()
        if m:
            return m.upper()
        name = (render.get("name") or "").strip()
        if not name:
            return ""
        token = re.split(r"[_\-\s]+", name, maxsplit=1)[0]
        return token.upper()


def load_routing(path: str | Path) -> PublishRouting:
    p = Path(path)
    if not p.exists():
        logger.warning("路由配置不存在，返回空路由 - {}", p)
        return PublishRouting(windows=[])
    data = json.loads(p.read_text(encoding="utf-8"))
    windows = [
        WindowConfig(
            id=w["id"],
            name=w.get("name", w["id"]),
            region=w.get("region", ""),
            profile_id=w.get("profile_id", ""),
            voice_profile=w.get("voice_profile", ""),
            platforms=w.get("platforms", ["tiktok", "shopee"]),
            models=w.get("models", []),
            enabled=w.get("enabled", True),
        )
        for w in data.get("windows", [])
    ]
    routing = PublishRouting(
        windows=windows,
        regions=data.get("regions", {}),
        tiktok_upload_url=data.get("tiktok_upload_url", _DEFAULT_TIKTOK_UPLOAD),
    )
    logger.info(
        "发布路由加载 - {} 个窗口（就绪 {}）",
        len(windows), sum(1 for w in windows if w.ready),
    )
    return routing
