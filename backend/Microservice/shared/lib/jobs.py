"""
Phase 3 - Background job queue.

Heavy work (large ingestion, workbook export) should not block an HTTP request.
This module gives a tiny, uniform job API that every service can use:

    from jobs import submit, get_status
    job_id = submit(my_slow_function, arg1, arg2)      # returns immediately
    ...
    get_status(job_id)   # -> {"state": "running|done|error", "result": ..., ...}

Backends:
  - If REDIS_URL is set, job status is stored in Redis (survives across replicas /
    restarts and is visible to every service).
  - Otherwise it falls back to an in-process dict + thread pool (fine for local dev
    and single-replica services).

For true multi-worker execution you would run the same function set behind a
worker (e.g. ARQ/Celery) reading REDIS_URL; the status contract here stays identical.
"""
from __future__ import annotations

import json
import os
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

_POOL = ThreadPoolExecutor(max_workers=int(os.getenv("JOB_WORKERS", "4")))
_MEM: dict[str, dict] = {}
_redis_client = None


def _redis():
    global _redis_client
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    if _redis_client is None:
        import redis  # imported lazily so the dep is optional
        _redis_client = redis.from_url(url, decode_responses=True)
    return _redis_client


def _key(job_id: str) -> str:
    return f"kavachio:job:{job_id}"


def _write(job_id: str, data: dict) -> None:
    r = _redis()
    if r is not None:
        r.set(_key(job_id), json.dumps(data), ex=int(os.getenv("JOB_TTL_SEC", "86400")))
    else:
        _MEM[job_id] = data


def get_status(job_id: str) -> dict | None:
    r = _redis()
    if r is not None:
        raw = r.get(_key(job_id))
        return json.loads(raw) if raw else None
    return _MEM.get(job_id)


def submit(fn, *args, **kwargs) -> str:
    """Run fn(*args, **kwargs) in the background and return a job id immediately."""
    job_id = uuid.uuid4().hex
    _write(job_id, {"state": "queued", "result": None, "error": None})

    def _runner():
        _write(job_id, {"state": "running", "result": None, "error": None})
        try:
            result = fn(*args, **kwargs)
            _write(job_id, {"state": "done", "result": result, "error": None})
        except Exception as exc:  # noqa: BLE001
            _write(job_id, {
                "state": "error",
                "result": None,
                "error": str(exc),
                "trace": traceback.format_exc(),
            })

    _POOL.submit(_runner)
    return job_id


def install_jobs_api(app) -> None:
    """Add a generic GET /jobs/{job_id} status endpoint to a FastAPI app."""
    from fastapi import HTTPException

    @app.get("/jobs/{job_id}", tags=["_jobs"])
    def job_status(job_id: str):
        st = get_status(job_id)
        if st is None:
            raise HTTPException(404, "job not found")
        return {"job_id": job_id, **st}
