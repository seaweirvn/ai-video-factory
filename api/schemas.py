from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ApiResponse(BaseModel):
    ok: bool = True
    data: Any | None = None
    error: str | None = None


class JobRef(BaseModel):
    ok: bool = True
    job_id: str
    kind: str
    status: str
