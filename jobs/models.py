from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(BaseModel):
    id: str
    kind: str
    status: JobStatus = JobStatus.pending
    trace_id: str = "-"
    progress: float = 0.0
    result: Any | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
