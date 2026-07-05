"""统一日志：loguru + trace_id。

- 全链路用 trace_id 串联（素材ID/成片ID/job_id）。
- 同时输出到控制台和按天切分的文件。
"""

from __future__ import annotations

import sys
from contextvars import ContextVar
from pathlib import Path

from loguru import logger

# 当前请求/任务的 trace_id，跨函数传递无需显式参数。
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


def set_trace_id(value: str) -> None:
    _trace_id.set(value or "-")


def get_trace_id() -> str:
    return _trace_id.get()


def _patch(record: dict) -> None:
    record["extra"].setdefault("trace_id", get_trace_id())


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.configure(patcher=_patch)

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>trace={extra[trace_id]}</cyan> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )

    logger.add(sys.stderr, level=level, format=fmt, enqueue=True)
    logger.add(
        Path(log_dir) / "api_{time:YYYY-MM-DD}.log",
        level=level,
        format=fmt,
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
    )
