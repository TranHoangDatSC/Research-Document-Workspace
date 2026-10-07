"""Entry point of the `worker` service: runs queued jobs (app/jobs.py).

    python -m app.worker           run jobs until stopped
    python -m app.worker --check   healthcheck: is a worker of this container registered

Same image and .env as `web`. Loads admin-saved settings and follows changes
(app/settings.py), so jobs use the LLM keys/models set at /admin/settings.
"""
import logging
import os
import socket
import sys

import redis

from app import jobs, settings


def connection():
    # Its own client without socket_timeout: the worker blocks on the queue
    # far longer than the 1 s timeout of the shared client (app/storage.py).
    return redis.Redis.from_url(os.environ["REDIS_URL"])


def check():
    from rq import Worker

    hostname = socket.gethostname()
    alive = any(w.hostname == hostname for w in Worker.all(connection=connection()))
    return 0 if alive else 1


def main():
    from rq import Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings.apply_saved_overrides()
    settings.start_listener()
    Worker([jobs.QUEUE], connection=connection()).work()


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv else main())
