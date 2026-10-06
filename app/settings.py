"""Technical/AI settings an admin edits at /admin/settings without a redeploy.

Saving a value stores it in the Postgres `app_settings` table (survives a
restart) and writes it into os.environ (next request sees it). Readers
(llm.py, media_ai.py, domains/) just read os.environ. Clearing a value
restores what .env gave this process at startup.

Assumes one uvicorn worker: each extra worker/replica has its own os.environ.
"""
import logging
import os

import psycopg
from fastapi import HTTPException

from app.repositories import users as repository

log = logging.getLogger("uvicorn.error")

# Editable keys; SECRET_KEYS are never sent back to the browser.
KEYS = (
    "LLM_PROVIDER", "LLM_MODEL", "LLM_MODELS", "LLM_API_KEY", "LLM_API_KEYS", "LLM_BASE_URL",
    "LLM_TIMEOUT_SECONDS", "LLM_THINKING_BUDGET", "APP_DOMAIN", "AI_MEDIA_ANALYSIS",
)
SECRET_KEYS = {"LLM_API_KEY", "LLM_API_KEYS"}

# Values from .env at import time, before any override is applied.
_ORIGINAL_ENV = {key: os.environ.get(key) for key in KEYS}


def apply_saved_overrides():
    """Startup hook (main.py lifespan): copy saved overrides into os.environ.
    Never raises; if Postgres is unreachable the app runs on .env alone."""
    for key in KEYS:
        try:
            value = repository.get_setting(key)
        except psycopg.Error as exc:
            log.warning("settings_startup_read_failed key=%s error=%s", key, type(exc).__name__)
            continue
        if value:
            os.environ[key] = value


def effective(key):
    """Current value, as llm.py/media_ai.py/domains/ see it."""
    return os.environ.get(key, "")


def is_overridden(key):
    """True when the value differs from .env (shown next to the field)."""
    return effective(key) != (_ORIGINAL_ENV.get(key) or "")


def update(key, value, admin_id):
    """Persists to Postgres and applies immediately. A blank value clears
    the override and restores the .env value this process started with."""
    if key not in KEYS:
        raise ValueError(f"Unknown setting: {key}")
    value = (value or "").strip()
    try:
        repository.set_setting(key, value, admin_id)
    except psycopg.Error as exc:
        log.warning("settings_write_failed key=%s error=%s", key, type(exc).__name__)
        raise HTTPException(503, "Lưu cấu hình thất bại; kiểm tra log máy chủ") from None
    if value:
        os.environ[key] = value
    elif _ORIGINAL_ENV.get(key) is not None:
        os.environ[key] = _ORIGINAL_ENV[key]
    else:
        os.environ.pop(key, None)
    # Never log `value`: LLM_API_KEY/LLM_API_KEYS are secrets.
    log.info("setting_changed key=%s admin_id=%s", key, admin_id)


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
