"""In-memory sliding-window rate limiter for account forms and AI questions.

Counts live in this process: correct with one uvicorn worker only. With
several workers or servers they must move to a shared store (Redis, see
docs/ha-tang.md).
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException

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


def check(bucket, request, user_id=None):
    """Counts this attempt; 429 once the bucket's limit is reached.
    `user_id` counts per account instead of per IP (signed-in endpoints)."""
    limit, window = LIMITS[bucket]
    key = (bucket, user_id if user_id is not None else client_ip(request))
    now = time.monotonic()
    with _lock:
        hits = _hits[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            retry_after = int(window - (now - hits[0])) + 1
            raise HTTPException(
                429, f"Thử quá nhiều lần. Vui lòng đợi khoảng {max(1, retry_after // 60)} phút rồi thử lại.",
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)


def reset():
    """Tests only."""
    with _lock:
        _hits.clear()
