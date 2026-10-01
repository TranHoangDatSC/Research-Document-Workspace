"""Small in-memory rate limiter for the public account forms (login, sign-up,
forgot password): slows password guessing and stops one client from spamming
sign-ups or reset emails.

Per process: fine for this deployment (one uvicorn worker). With several
workers or servers each keeps its own count — move this to Redis then.
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
}

_hits = defaultdict(deque)
_lock = threading.Lock()


def client_ip(request):
    """Behind Caddy (docker-compose.prod.yaml) every request comes from the
    proxy, so use the address Caddy appended last to X-Forwarded-For; Caddy
    replaces a client-supplied header unless trusted_proxies says otherwise."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def check(bucket, request):
    """Counts this attempt; 429 once the bucket's limit is reached."""
    limit, window = LIMITS[bucket]
    key = (bucket, client_ip(request))
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
