"""进程内异步 job 框架。

长耗时任务（剪辑/转码/发布）不阻塞 HTTP 请求：
- submit() 立即返回 job_id，任务丢到线程池后台执行。
- get() 供 /jobs/{id} 轮询状态与结果。

阶段 0 用内存存储；后续需要跨进程/持久化时可替换为 Redis/DB 而不改调用方。
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable

from loguru import logger

from app.logging import get_trace_id, set_trace_id
from jobs.models import Job, JobStatus


class JobManager:
    def __init__(self, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, func: Callable[["JobContext"], object]) -> Job:
        job_id = uuid.uuid4().hex
        trace_id = get_trace_id()
        job = Job(id=job_id, kind=kind, trace_id=trace_id)
        with self._lock:
            self._jobs[job_id] = job
        self._executor.submit(self._run, job_id, trace_id, func)
        logger.info("已提交 job - id={} kind={}", job_id, kind)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def _run(self, job_id: str, trace_id: str, func: Callable[["JobContext"], object]) -> None:
        set_trace_id(trace_id)
        self._update(job_id, status=JobStatus.running)
        ctx = JobContext(self, job_id)
        try:
            result = func(ctx)
            self._update(job_id, status=JobStatus.succeeded, result=result, progress=1.0)
            logger.info("job 完成 - id={}", job_id)
        except Exception as exc:  # noqa: BLE001 - 记录并落到 job 状态
            logger.exception("job 失败 - id={}", job_id)
            self._update(job_id, status=JobStatus.failed, error=str(exc))

    def _update(self, job_id: str, **changes: object) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = datetime.now(timezone.utc)


class JobContext:
    """任务体通过它回报进度。"""

    def __init__(self, manager: JobManager, job_id: str) -> None:
        self._manager = manager
        self.job_id = job_id

    def set_progress(self, value: float) -> None:
        self._manager._update(self.job_id, progress=max(0.0, min(1.0, value)))


@lru_cache
def get_job_manager() -> JobManager:
    return JobManager()
