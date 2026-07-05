"""FastAPI 服务入口。

职责：装配中间件（trace_id）、注册路由、暴露健康检查。
业务逻辑不在这里，全部在 services/ 下。
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from api.routes import register_routes
from app.config import get_settings
from app.logging import set_trace_id, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level)
    logger.info("服务启动 - {} env={}", settings.app_name, settings.app_env)
    yield
    logger.info("服务关闭 - {}", settings.app_name)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def trace_id_middleware(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
        set_trace_id(trace_id)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("未处理异常 - {} {}", request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": "internal_error", "trace_id": trace_id},
            )
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.get("/health", tags=["system"])
    async def health():
        return {"ok": True, "app": settings.app_name, "env": settings.app_env}

    register_routes(app)
    return app


app = create_app()
