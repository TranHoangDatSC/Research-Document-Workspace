"""Technical/AI settings an admin edits at /admin/settings without a redeploy.

Saving a value stores it in the Postgres `app_settings` table (survives a
restart) and writes it into os.environ (next request sees it). Readers
(llm.py, media_ai.py, domains/) just read os.environ. Clearing a value
restores what .env gave this process at startup.

With several uvicorn workers each has its own os.environ, so a save also
publishes the key on the Redis channel CHANNEL; every worker runs
start_listener() and re-reads that key from Postgres. Only the key name is
published, never the value (API keys are secrets). After a lost Redis
connection the listener re-reads every key, so missed messages converge.
"""
import logging
import os
import threading

import psycopg
from fastapi import HTTPException

from app.repositories import users as repository
from app.storage import redis_client

log = logging.getLogger("uvicorn.error")

# Editable keys; SECRET_KEYS are never sent back to the browser.
KEYS = (
    "LLM_PROVIDER", "LLM_MODEL", "LLM_MODELS", "LLM_API_KEY", "LLM_API_KEYS", "LLM_BASE_URL",
    "LLM_TIMEOUT_SECONDS", "LLM_THINKING_BUDGET", "APP_DOMAIN", "AI_MEDIA_ANALYSIS",
)
SECRET_KEYS = {"LLM_API_KEY", "LLM_API_KEYS"}
CHANNEL = "settings:changed"
RECONNECT_SECONDS = 5

# Values from .env at import time, before any override is applied.
_ORIGINAL_ENV = {key: os.environ.get(key) for key in KEYS}
_stop = threading.Event()


def _apply(key, value):
    """Saved value wins; blank restores the .env value (or removes the key)."""
    if value:
        os.environ[key] = value
    elif _ORIGINAL_ENV.get(key) is not None:
        os.environ[key] = _ORIGINAL_ENV[key]
    else:
        os.environ.pop(key, None)


def reload_key(key):
    """Re-reads one key from Postgres into os.environ. Never raises."""
    if key not in KEYS:
        return
    try:
        value = repository.get_setting(key)
    except psycopg.Error as exc:
        log.warning("settings_read_failed key=%s error=%s", key, type(exc).__name__)
        return
    _apply(key, value)


def apply_saved_overrides():
    """Loads every saved override into os.environ: at startup (main.py
    lifespan) and after the listener reconnects. Never raises; if Postgres
    is unreachable the app runs on .env alone."""
    for key in KEYS:
        reload_key(key)


def effective(key):
    """Current value, as llm.py/media_ai.py/domains/ see it."""
    return os.environ.get(key, "")


def is_overridden(key):
    """True when the value differs from .env (shown next to the field)."""
    return effective(key) != (_ORIGINAL_ENV.get(key) or "")


def update(key, value, admin_id):
    """Persists to Postgres, applies in this worker and tells the others.
    A blank value clears the override."""
    if key not in KEYS:
        raise ValueError(f"Unknown setting: {key}")
    value = (value or "").strip()
    try:
        repository.set_setting(key, value, admin_id)
    except psycopg.Error as exc:
        log.warning("settings_write_failed key=%s error=%s", key, type(exc).__name__)
        raise HTTPException(503, "Lưu cấu hình thất bại; kiểm tra log máy chủ") from None
    _apply(key, value)
    _publish(key)
    # Never log `value`: LLM_API_KEY/LLM_API_KEYS are secrets.
    log.info("setting_changed key=%s admin_id=%s", key, admin_id)


def _publish(key):
    """Best-effort: the value is already saved; a worker that misses this
    message catches up when its listener reconnects."""
    client = redis_client()
    if client is None:
        return
    try:
        client.publish(CHANNEL, key)
    except Exception as exc:
        log.warning("settings_publish_failed key=%s error=%s", key, type(exc).__name__)


def handle_message(message):
    """One pub/sub message -> reload the key it names."""
    if not message or message.get("type") != "message":
        return
    key = message.get("data")
    if isinstance(key, bytes):
        key = key.decode("utf-8", "replace")
    reload_key(key)


def _listen():
    while not _stop.is_set():
        client = redis_client()
        if client is None:
            return
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        try:
            pubsub.subscribe(CHANNEL)
            apply_saved_overrides()  # catch up on anything missed while disconnected
            while not _stop.is_set():
                handle_message(pubsub.get_message(timeout=1.0))
        except Exception as exc:
            log.warning("settings_listener_disconnected error=%s", type(exc).__name__)
            _stop.wait(RECONNECT_SECONDS)
        finally:
            try:
                pubsub.close()
            except Exception:
                pass


def start_listener():
    """Startup hook (main.py lifespan). No-op without REDIS_URL."""
    if redis_client() is None:
        return None
    _stop.clear()
    thread = threading.Thread(target=_listen, name="settings-listener", daemon=True)
    thread.start()
    return thread


def stop_listener():
    _stop.set()


def masked(key):
    """Display-safe stand-in for a secret: last 4 chars, or a key count."""
    value = effective(key)
    if not value:
        return None
    if key == "LLM_API_KEYS":
        count = len([v for v in value.replace("\n", ",").split(",") if v.strip()])
        return f"Đang lưu {count} khóa" if count else None
    return "••••" + value[-4:] if len(value) > 4 else "••••"


# ----- API key list (/admin/keys, unlocked by an emailed link: see
# services/auth.request_key_access). Edits LLM_API_KEYS only; the single
# LLM_API_KEY stays a .env-only fallback. -----

def _mask_one(value):
    return ("••••" + value[-4:]) if len(value) > 4 else "••••"


def _key_list():
    raw = effective("LLM_API_KEYS")
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


def list_keys():
    """[{"index":, "masked":}, ...] — real values never leave this module."""
    return [{"index": i, "masked": _mask_one(k)} for i, k in enumerate(_key_list())]


def add_key(value, admin_id):
    value = (value or "").strip()
    if not value:
        raise HTTPException(422, "Key không được để trống")
    keys = _key_list()
    if value in keys:
        raise HTTPException(409, "Key này đã có trong danh sách")
    keys.append(value)
    update("LLM_API_KEYS", ",".join(keys), admin_id)


def delete_key(index, admin_id):
    keys = _key_list()
    if not (0 <= index < len(keys)):
        raise HTTPException(404, "Không tìm thấy key")
    keys.pop(index)
    update("LLM_API_KEYS", ",".join(keys), admin_id)


def replace_key(index, value, admin_id):
    value = (value or "").strip()
    if not value:
        raise HTTPException(422, "Key không được để trống")
    keys = _key_list()
    if not (0 <= index < len(keys)):
        raise HTTPException(404, "Không tìm thấy key")
    keys[index] = value
    update("LLM_API_KEYS", ",".join(keys), admin_id)
