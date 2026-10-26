"""Phase 1: durable job queue — idempotency, SKIP LOCKED claims, leases, cancel, resume."""

import threading

import psycopg2
import pytest

import jobs
import worker as job_worker
from db import DB_CONFIG
from jobs import JobCancelled
from tests.conftest import requires_db

pytestmark = requires_db


def test_enqueue_is_idempotent_for_active_key(db_conn, job_cleanup):
    a = jobs.enqueue(db_conn, "test_refresh", {"n": 1}, idempotency_key="test_refresh")
    b = jobs.enqueue(db_conn, "test_refresh", {"n": 2}, idempotency_key="test_refresh")
    assert a["id"] == b["id"]
    assert a["existing"] is False and b["existing"] is True
    jobs.complete(db_conn, str(a["id"]), {"ok": True})
    c = jobs.enqueue(db_conn, "test_refresh", {"n": 3}, idempotency_key="test_refresh")
    assert c["id"] != a["id"], "a finished job must not block a new one"


def test_concurrent_enqueue_creates_exactly_one_job(job_cleanup, db_conn):
    results = []

    def go():
        conn = psycopg2.connect(**DB_CONFIG)
        try:
            results.append(str(jobs.enqueue(conn, "test_concurrent", {}, idempotency_key="test_concurrent")["id"]))
        finally:
            conn.close()

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM background_jobs WHERE job_type = 'test_concurrent' AND status IN ('queued','running')")
        assert cur.fetchone()[0] == 1


def test_claim_uses_skip_locked_so_two_workers_never_share_a_job(db_conn, job_cleanup):
    ids = {str(jobs.enqueue(db_conn, "test_claim", {"i": i})["id"]) for i in range(3)}
    c1, c2 = psycopg2.connect(**DB_CONFIG), psycopg2.connect(**DB_CONFIG)
    try:
        j1 = jobs.claim_next(c1, "w1", ["test_claim"])
        j2 = jobs.claim_next(c2, "w2", ["test_claim"])
        assert j1["id"] != j2["id"]
        assert {str(j1["id"]), str(j2["id"])} <= ids
        assert j1["status"] == "running" and j1["attempts"] == 1 and j1["lease_expires_at"] is not None
    finally:
        c1.close(); c2.close()


def test_expired_lease_is_reclaimed_and_requeued(db_conn, job_cleanup):
    job = jobs.enqueue(db_conn, "test_lease", {}, max_attempts=2)
    claimed = jobs.claim_next(db_conn, "dead-worker", ["test_lease"])
    assert claimed["id"] == job["id"]
    with db_conn.cursor() as cur:
        cur.execute("UPDATE background_jobs SET lease_expires_at = NOW() - INTERVAL '5 minutes' WHERE id = %s", (str(job["id"]),))
    db_conn.commit()
    assert jobs.reclaim_expired(db_conn) >= 1
    row = jobs.get_job(db_conn, str(job["id"]))
    assert row["status"] == "queued" and row["worker_id"] is None and "lease expired" in row["error"]
    # second death exhausts attempts -> failed
    jobs.claim_next(db_conn, "dead-worker-2", ["test_lease"])
    with db_conn.cursor() as cur:
        cur.execute("UPDATE background_jobs SET lease_expires_at = NOW() - INTERVAL '5 minutes' WHERE id = %s", (str(job["id"]),))
    db_conn.commit()
    jobs.reclaim_expired(db_conn)
    assert jobs.get_job(db_conn, str(job["id"]))["status"] == "failed"


def test_cancel_queued_is_immediate_and_running_is_cooperative(db_conn, job_cleanup):
    queued = jobs.enqueue(db_conn, "test_cancel", {"a": 1})
    assert jobs.cancel(db_conn, str(queued["id"]))["status"] == "cancelled"

    running = jobs.enqueue(db_conn, "test_cancel", {"a": 2})
    jobs.claim_next(db_conn, "w", ["test_cancel"])
    out = jobs.cancel(db_conn, str(running["id"]))
    assert out["status"] == "running" and out["cancel_requested"] is True
    assert jobs.heartbeat(db_conn, str(running["id"])) is False, "heartbeat must report cancellation"
    progress = jobs.JobProgress(db_conn, jobs.get_job(db_conn, str(running["id"])))
    with pytest.raises(JobCancelled):
        progress.checkpoint()


def test_progress_is_persisted_and_survives_reconnect(db_conn, job_cleanup):
    job = jobs.enqueue(db_conn, "test_progress", {})
    progress = jobs.JobProgress(db_conn, job, min_interval=0)
    progress.update({"phase": "x", "current": 3, "total": 10})
    fresh = psycopg2.connect(**DB_CONFIG)
    try:
        assert jobs.get_job(fresh, str(job["id"]))["progress"] == {"phase": "x", "current": 3, "total": 10}
    finally:
        fresh.close()


def test_worker_resumes_after_crash_without_redoing_completed_items(db_conn, job_cleanup):
    """Handler processes items and records them in progress['done']; the first
    attempt dies half way. The second attempt must skip what was already done."""
    processed: list[str] = []
    crash = {"armed": True}

    def handler(conn, job, progress):
        done = set(progress.get("done") or [])
        for item in job["params"]["items"]:
            if item in done:
                continue
            processed.append(item)
            done.add(item)
            progress.data["done"] = sorted(done)
            progress.checkpoint()
            if crash["armed"] and len(done) == 2:
                crash["armed"] = False
                raise RuntimeError("simulated worker crash")
        return {"processed": sorted(done)}

    job_worker.register("test_resume", handler)
    job = jobs.enqueue(db_conn, "test_resume", {"items": ["A", "B", "C", "D"]}, max_attempts=2)
    w = job_worker.JobWorker(lambda: psycopg2.connect(**DB_CONFIG), worker_id="t")
    assert w.run_once() is True  # crashes after A, B -> requeued
    mid = jobs.get_job(db_conn, str(job["id"]))
    assert mid["status"] == "queued" and mid["progress"]["done"] == ["A", "B"]
    assert w.run_once() is True  # resumes with C, D
    final = jobs.get_job(db_conn, str(job["id"]))
    assert final["status"] == "succeeded"
    assert processed == ["A", "B", "C", "D"], "no item processed twice"
    assert final["result"]["processed"] == ["A", "B", "C", "D"]


def test_worker_marks_job_cancelled_when_handler_raises_jobcancelled(db_conn, job_cleanup):
    job_worker.register("test_cancel_handler", lambda conn, job, progress: (_ for _ in ()).throw(JobCancelled("stop")))
    job = jobs.enqueue(db_conn, "test_cancel_handler", {})
    job_worker.JobWorker(lambda: psycopg2.connect(**DB_CONFIG), worker_id="t").run_once()
    assert jobs.get_job(db_conn, str(job["id"]))["status"] == "cancelled"


def test_queue_stats_shape(db_conn):
    stats = jobs.queue_stats(db_conn)
    assert {"queued", "running", "failed_24h", "succeeded_24h", "stale_leases"} <= set(stats)
