"""Technical/AI settings an admin can change from /admin/settings without a
redeploy — same mechanism app/services/auth.py already uses for the sign-up
toggle (the app_settings Postgres table, get_setting/set_setting), just with
more keys.

Every reader of these — app/llm.py, app/domains/, app/media_ai.py — keeps
reading plain os.environ exactly as before; nothing there changes. Instead,
saving a setting here does two things: persists it to Postgres (survives a
restart) and writes it into this process's os.environ right away (takes
effect on the very next request, no restart needed). Clearing a setting
restores exactly the value this process started with — captured once, below,
before any admin override can have run.

Single uvicorn worker only (see app/ratelimit.py's docstring for the same
caveat): with several workers or replicas, each process keeps its own
os.environ and would need apply_saved_overrides() called in it.
"""
import logging
import os

import psycopg
from fastapi import HTTPException

from app.repositories import users as repository

log = logging.getLogger("uvicorn.error")

# Every setting this page can edit, and whether its value is a secret that
# must never be sent back to the browser once saved.
KEYS = (
    "LLM_PROVIDER", "LLM_MODEL", "LLM_MODELS", "LLM_API_KEY", "LLM_API_KEYS", "LLM_BASE_URL",
    "LLM_TIMEOUT_SECONDS", "LLM_THINKING_BUDGET", "APP_DOMAIN", "AI_MEDIA_ANALYSIS",
)
SECRET_KEYS = {"LLM_API_KEY", "LLM_API_KEYS"}

# What each of these was when this process started, straight from the
# container's .env (via docker-compose's env_file=) — read once at import,
# before any admin override can run.
_ORIGINAL_ENV = {key: os.environ.get(key) for key in KEYS}


def apply_saved_overrides():
    """Called once at server startup (app/main.py lifespan): pulls every
    admin-saved override out of Postgres into os.environ. Never raises — a
    database that isn't ready yet just means this boots on .env alone, the
    same as before this module existed."""
    for key in KEYS:
        try:
            value = repository.get_setting(key)
        except psycopg.Error as exc:
            log.warning("settings_startup_read_failed key=%s error=%s", key, type(exc).__name__)
            continue
        if value:
            os.environ[key] = value


def effective(key):
    """What's actually in os.environ right now — exactly what llm.py,
    app/domains/ and media_ai.py already see when they read it themselves."""
    return os.environ.get(key, "")


def is_overridden(key):
    """Whether an admin changed this from the UI, as opposed to it still
    being whatever .env set at startup — shown next to the field so nobody
    has to guess where the current value is coming from."""
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
    """A safe-to-render stand-in for a secret's current value — never the
    real thing, and never even its length for a list (that narrows down how
    many keys are configured more than a viewer needs to know)."""
    value = effective(key)
    if not value:
        return None
    if key == "LLM_API_KEYS":
        count = len([v for v in value.replace("\n", ",").split(",") if v.strip()])
        return f"Đang lưu {count} khóa" if count else None
    return "••••" + value[-4:] if len(value) > 4 else "••••"


# ----- API key CRUD (/admin/keys, gated behind the emailed re-auth link —
# app/services/auth.py's request_key_access/confirm_key_access and
# app/auth.py's key-access cookie). LLM_API_KEYS is the one list these
# operate on; LLM_API_KEY (singular) stays as a plain .env-only fallback for
# a simple one-key setup and isn't editable from this page. -----

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
