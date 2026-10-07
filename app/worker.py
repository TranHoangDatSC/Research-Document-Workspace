"""Entry point of the `worker` service: runs queued jobs (app/jobs.py).

    python -m app.worker           run jobs until stopped
    python -m app.worker --check   healthcheck: is this container's worker alive

Same image and .env as `web`. Loads admin-saved settings and follows changes
(app/settings.py), so jobs use the LLM keys/models set at /admin/settings.
"""
import logging
import os
import socket
import sys
from pathlib import Path
from uuid import uuid4

import redis

from app import jobs, settings

# The worker's name, for the healthcheck (a separate process in the container).
NAME_FILE = Path("/tmp/rq-worker-name")


def connection():
    # Its own client without socket_timeout: the worker blocks on the queue
    # far longer than the 1 s timeout of the shared client (app/storage.py).
    return redis.Redis.from_url(os.environ["REDIS_URL"])


def check():
    """Healthy while the worker's heartbeat key exists (RQ renews it with a
    TTL). Not Worker.all(): that reads the rq:workers set, which a Redis
    restart wipes for good (no persistence) while the heartbeat key comes back
    with the next heartbeat."""
    from rq import Worker

    try:
        name = NAME_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return 1
    return 0 if connection().exists(Worker.redis_worker_namespace_prefix + name) else 1


def main():
    from rq import Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings.apply_saved_overrides()
    settings.start_listener()
    # Unique per start: RQ refuses a name that still has a live heartbeat key,
    # which a killed (not stopped) container would leave behind.
    name = f"{socket.gethostname()}-{uuid4().hex[:8]}"
    NAME_FILE.write_text(name, encoding="utf-8")
    Worker([jobs.QUEUE], connection=connection(), name=name).work()


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv else main())
