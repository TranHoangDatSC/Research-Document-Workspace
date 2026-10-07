"""Background jobs on Redis (RQ) for work that takes minutes, so a request
returns at once. The `worker` service (app/worker.py) runs them.

enqueue() returns False when there is no queue (no REDIS_URL) or it can't
be reached; callers then do the work inline, as before the queue existed.
`rq` is imported on first use, like `redis` in app/storage.py.
"""
import logging

from app.storage import redis_client

log = logging.getLogger("uvicorn.error")

QUEUE = "media"
# Gemini may take up to 10 min to process a video (media_ai) plus the upload.
JOB_TIMEOUT_SECONDS = 15 * 60


def enqueue(function_path, *args, job_id):
    """Queues `function_path(*args)` (dotted path, run in the worker)."""
    client = redis_client()
    if client is None:
        return False
    try:
        from rq import Queue

        Queue(QUEUE, connection=client).enqueue(
            function_path, *args, job_id=job_id,
            job_timeout=JOB_TIMEOUT_SECONDS, result_ttl=3600, failure_ttl=24 * 3600,
        )
    except Exception as exc:
        log.warning("job_enqueue_failed function=%s error=%s", function_path, type(exc).__name__)
        return False
    return True
