from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Callable
from uuid import uuid4


ProgressCallback = Callable[[dict[str, object] | None, str | None], None]
JobFunction = Callable[[ProgressCallback], dict[str, object]]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict[str, object] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    result: dict[str, object] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "logs": self.logs[-80:],
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self, max_workers: int = 2) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._jobs: dict[str, Job] = {}
        self._futures: dict[str, Future[dict[str, object]]] = {}
        self._lock = Lock()

    def submit(self, kind: str, function: JobFunction) -> Job:
        job = Job(id=uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job

        future = self._executor.submit(self._run, job.id, function)
        with self._lock:
            self._futures[job.id] = future
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)

    def update(
        self,
        job_id: str,
        progress: dict[str, object] | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if progress:
                job.progress.update(progress)
            if message:
                job.logs.append(f"{utc_now()} {message}")

    def _run(self, job_id: str, function: JobFunction) -> dict[str, object]:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.started_at = utc_now()
            job.logs.append(f"{job.started_at} started {job.kind}")

        def progress(data: dict[str, object] | None = None, message: str | None = None) -> None:
            self.update(job_id, data, message)

        try:
            result = function(progress)
        except Exception as exc:
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.finished_at = utc_now()
                job.error = f"{type(exc).__name__}: {exc}"
                job.logs.append(f"{job.finished_at} failed: {job.error}")
            raise

        with self._lock:
            job = self._jobs[job_id]
            job.status = "completed"
            job.finished_at = utc_now()
            job.result = result
            job.logs.append(f"{job.finished_at} completed {job.kind}")
        return result
