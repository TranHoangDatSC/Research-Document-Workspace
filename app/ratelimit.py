"""Sliding-window rate limiter for account forms and AI questions.

Counts live in Redis (shared by every worker and kept across `web`
restarts) when REDIS_URL is set. Without Redis, or while it is unreachable,
counts fall back to this process's memory, so a Redis outage never blocks
sign-in. See docs/infrastructure.md, section 13.
"""
import logging
import threading
import time
from collections import defaultdict, deque
from uuid import uuid4

from fastapi import HTTPException

from app.storage import redis_client

log = logging.getLogger("uvicorn.error")

# bucket -> (max attempts, window in seconds)
LIMITS = {
    "login": (20, 5 * 60),
    "signup": (5, 60 * 60),
    "forgot-password": (5, 15 * 60),
    "reset-password": (10, 15 * 60),
    "verify-resend": (5, 15 * 60),
    # Each question costs LLM quota.
    "ask": (20, 5 * 60),
}

_hits = defaultdict(deque)
_lock = threading.Lock()


def client_ip(request):
    """Behind Caddy, the last X-Forwarded-For entry (added by Caddy)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _memory_hit(key, limit, window):
    """Records the attempt in process memory; seconds to wait, or None if allowed."""
    now = time.monotonic()
    with _lock:
        hits = _hits[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return window - (now - hits[0])
        hits.append(now)
    return None


def _redis_hit(client, key, limit, window):
    """Same as _memory_hit over a Redis sorted set (member scored by time).
    Not atomic across the two round trips: two simultaneous requests may both
    pass at the limit, which is acceptable for a rate limit."""
    now = time.time()
    pipe = client.pipeline()
    pipe.zremrangebyscore(key, 0, now - window)
    pipe.zcard(key)
    pipe.zrange(key, 0, 0, withscores=True)
    _, count, oldest = pipe.execute()
    if count >= limit:
        return window - (now - oldest[0][1]) if oldest else window
    pipe = client.pipeline()
    pipe.zadd(key, {f"{now}:{uuid4().hex}": now})
    pipe.expire(key, window)
    pipe.execute()
    return None


def check(bucket, request, user_id=None):
    """Counts this attempt; 429 once the bucket's limit is reached.
    `user_id` counts per account instead of per IP (signed-in endpoints)."""
    limit, window = LIMITS[bucket]
    who = user_id if user_id is not None else client_ip(request)
    wait = None
    client = redis_client()
    if client is not None:
        try:
            wait = _redis_hit(client, f"rl:{bucket}:{who}", limit, window)
        except Exception as exc:
            log.warning("ratelimit_redis_unavailable error=%s", type(exc).__name__)
            client = None
    if client is None:
        wait = _memory_hit((bucket, who), limit, window)
    if wait is not None:
        retry_after = int(wait) + 1
        raise HTTPException(
            429, f"Thử quá nhiều lần. Vui lòng đợi khoảng {max(1, retry_after // 60)} phút rồi thử lại.",
            headers={"Retry-After": str(retry_after)},
        )


def reset():
    """Tests only: clears the in-memory counts."""
    with _lock:
        _hits.clear()
