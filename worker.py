"""
Background job worker.

Runs in-process inside the API by default (``RUN_WORKER=true``) or as a
separate process::

    python worker.py

Handlers are registered by the module that owns the logic (api.py) with
``register(job_type, fn)``. A handler receives ``(conn, job, progress)`` and
returns a JSON-serialisable result dict. Raising ``JobCancelled`` marks the job
cancelled; any other exception marks it failed (re-queued while attempts remain).
"""

import logging
import os
import socket
import threading
import time
import uuid
from typing import Callable

import jobs
from jobs import JobCancelled, JobProgress

logger = logging.getLogger("worker")

_HANDLERS: dict[str, Callable] = {}
POLL_SECONDS = float(os.getenv("WORKER_POLL_SECONDS", "2"))
RECLAIM_EVERY_SECONDS = 60


def register(job_type: str, handler: Callable) -> None:
    _HANDLERS[job_type] = handler


def registered_types() -> list[str]:
    return sorted(_HANDLERS)


class JobWorker:
    def __init__(self, connect: Callable, *, worker_id: str | None = None, on_failure: Callable | None = None):
        self._connect = connect
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_failure = on_failure
        self._last_reclaim = 0.0

    # -- lifecycle ----------------------------------------------------------
    def start_in_thread(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run_forever, name="job-worker", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def run_forever(self) -> None:
        logger.info("worker %s started (types: %s)", self.worker_id, ", ".join(registered_types()))
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("worker loop error")
                worked = False
            if not worked:
                self._stop.wait(POLL_SECONDS)
        logger.info("worker %s stopped", self.worker_id)

    # -- one unit of work ---------------------------------------------------
    def run_once(self) -> bool:
        """Claim and run one job. Returns True if a job was processed."""
        conn = self._connect()
        try:
            if time.monotonic() - self._last_reclaim > RECLAIM_EVERY_SECONDS:
                jobs.reclaim_expired(conn)
                self._last_reclaim = time.monotonic()
            job = jobs.claim_next(conn, self.worker_id, registered_types())
            if not job:
                return False
            self._execute(conn, job)
            return True
        finally:
            conn.close()

    def _execute(self, conn, job: dict) -> None:
        job_id = str(job["id"])
        handler = _HANDLERS.get(job["job_type"])
        progress = JobProgress(conn, job)
        logger.info("job %s (%s) started by %s", job_id, job["job_type"], self.worker_id)
        if handler is None:
            jobs.fail(conn, job_id, f"no handler registered for {job['job_type']}", retry=False)
            return
        try:
            result = handler(conn, job, progress) or {}
            progress.flush(force=True)
            jobs.complete(conn, job_id, result, progress.data)
            logger.info("job %s succeeded", job_id)
        except JobCancelled:
            conn.rollback()
            progress.data["message"] = "Cancelled by operator"
            jobs.mark_cancelled(conn, job_id, progress.data)
            logger.info("job %s cancelled", job_id)
        except Exception as exc:
            conn.rollback()
            logger.exception("job %s failed", job_id)
            status = jobs.fail(conn, job_id, f"{type(exc).__name__}: {exc}", progress=progress.data)
            if status == "failed" and self._on_failure:
                try:
                    self._on_failure(conn, job, exc)
                except Exception:
                    logger.exception("failure hook error for job %s", job_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import api  # registers handlers and creates the pool helpers

    api.init_runtime()
    worker = JobWorker(api.worker_connect, on_failure=api.on_job_failure)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
