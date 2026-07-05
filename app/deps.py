"""FastAPI 依赖：配置注入 + API Key 校验（n8n -> API 的简单鉴权）。"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.config import Settings, get_settings


def settings_dep() -> Settings:
    return get_settings()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    # dev 环境或未配置 key 时不强制校验，方便本地联调。
    if settings.app_env != "dev" and settings.api_key and settings.api_key != "change-me":
        if x_api_key != settings.api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid api key",
            )
