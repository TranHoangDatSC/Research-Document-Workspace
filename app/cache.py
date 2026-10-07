"""Optional Redis cache for small JSON values.

Every failure reads as a cache miss, so callers always fall back to the
real source: without REDIS_URL, or while Redis is down, the app behaves as
if this module did nothing. While Redis is down the warning is logged at
most once a minute, since a cache read happens on every request.
"""
import json
import logging
import time

from app.storage import redis_client

log = logging.getLogger("uvicorn.error")
WARN_INTERVAL_SECONDS = 60
_last_warning = 0.0


def _warn(stage, exc):
    global _last_warning
    now = time.monotonic()
    if now - _last_warning >= WARN_INTERVAL_SECONDS:
        _last_warning = now
        log.warning("cache_redis_unavailable stage=%s error=%s", stage, type(exc).__name__)


def get(key):
    """The cached value, or None on a miss or any Redis problem."""
    client = redis_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
        return json.loads(raw) if raw is not None else None
    except Exception as exc:
        _warn("get", exc)
        return None


def set(key, value, ttl_seconds):
    client = redis_client()
    if client is None:
        return
    try:
        client.set(key, json.dumps(value), ex=ttl_seconds)
    except Exception as exc:
        _warn("set", exc)


def delete(key):
    client = redis_client()
    if client is None:
        return
    try:
        client.delete(key)
    except Exception as exc:
        _warn("delete", exc)
