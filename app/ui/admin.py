"""Admin-only pages: account management, AI usage stats, technical/AI settings."""
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Request, Form, HTTPException, Query
from fastapi.responses import RedirectResponse

from app import auth as core_auth, domains, settings as app_settings
from app.services import auth as service
from app.services import usage_stats
from app.ui.routes import email_link_base, is_https, pager_context, render

router = APIRouter(prefix="/admin", include_in_schema=False)

USERS_PAGE_SIZE = 20


def require_admin(request: Request):
    user = request.state.user
    if user["role"] != "admin":
        raise HTTPException(403, "Chỉ quản trị viên mới truy cập được trang này")
    return user


def has_key_access(request: Request, user_id):
    return core_auth.verify_key_access_token(request.cookies.get(core_auth.KEY_ACCESS_COOKIE), user_id)


def require_key_access(request: Request, user_id):
    if not has_key_access(request, user_id):
        raise HTTPException(403, "Cần xác thực lại qua email trước khi thao tác với API key")


@router.get("/users")
def users_page(request: Request, page: int = Query(default=1, ge=1), q: str = Query(default="", max_length=200)):
    current_user = require_admin(request)
    query = q.strip() or None
    total = service.count_users(query)
    total_pages = max(1, -(-total // USERS_PAGE_SIZE))
    page = min(page, total_pages)
    offset = (page - 1) * USERS_PAGE_SIZE
    qs = f"&q={quote(query)}" if query else ""
    return render(
        request, "admin_users.html", current_user=current_user,
        users=service.list_users(USERS_PAGE_SIZE, offset, query),
        total_users=total, search_query=q.strip(), user_stats=service.user_stats(),
        signup_open=service.signup_enabled(), signup_locked=service.signup_locked_by_env(),
        **pager_context("Phân trang tài khoản", page, total_pages, lambda n: f"/admin/users?page={n}{qs}"),
    )


@router.post("/users")
def create_user(
    request: Request,
    username: Annotated[str, Form(max_length=50)] = "",
    password: Annotated[str, Form(max_length=200)] = "",
    role: Annotated[str, Form()] = "user",
    email: Annotated[str, Form(max_length=254)] = "",
):
    require_admin(request)
    service.create_user(username.strip(), password, role, email)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/role")
def change_role(request: Request, user_id: UUID, role: Annotated[str, Form()] = "user"):
    current_user = require_admin(request)
    service.set_role(user_id, role, current_user["user_id"])
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/active")
def toggle_active(request: Request, user_id: UUID, is_active: Annotated[str, Form()] = "true"):
    current_user = require_admin(request)
    service.set_active(user_id, is_active == "true", current_user["user_id"])
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/verify")
def verify_user(request: Request, user_id: UUID):
    """For when a verification email never arrives."""
    require_admin(request)
    service.mark_email_verified(user_id)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/settings/signup")
def set_signup(request: Request, open: Annotated[str, Form()] = "false"):
    current_user = require_admin(request)
    service.set_signup_open(open == "true", current_user["user_id"])
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/stats")
def stats_page(request: Request, range: str = Query(default="", max_length=10)):
    current_user = require_admin(request)
    range_name = range if range in usage_stats.RANGES else usage_stats.DEFAULT_RANGE
    return render(
        request, "admin_stats.html", current_user=current_user,
        report=usage_stats.report(range_name), ranges=usage_stats.RANGES,
    )


@router.get("/settings")
def settings_page(request: Request):
    current_user = require_admin(request)
    return render(
        request, "admin_settings.html", current_user=current_user,
        keys=app_settings.KEYS, secret_keys=app_settings.SECRET_KEYS,
        effective={k: app_settings.effective(k) for k in app_settings.KEYS},
        masked={k: app_settings.masked(k) for k in app_settings.SECRET_KEYS},
        overridden={k: app_settings.is_overridden(k) for k in app_settings.KEYS},
        domain_options=domains.available(),
    )


@router.post("/settings")
def update_settings(request: Request, key: Annotated[str, Form(max_length=50)], value: Annotated[str, Form(max_length=2000)] = ""):
    current_user = require_admin(request)
    if key not in app_settings.KEYS:
        raise HTTPException(422, "Cấu hình không hợp lệ")
    # Secret fields are never pre-filled, so blank means "no change".
    if key in app_settings.SECRET_KEYS and not value.strip():
        return RedirectResponse("/admin/settings", status_code=303)
    app_settings.update(key, value, current_user["user_id"])
    return RedirectResponse("/admin/settings", status_code=303)


@router.get("/keys")
def keys_page(request: Request, sent: Annotated[str, Query()] = ""):
    current_user = require_admin(request)
    unlocked = has_key_access(request, current_user["user_id"])
    return render(
        request, "admin_keys.html", current_user=current_user,
        unlocked=unlocked, keys=app_settings.list_keys() if unlocked else [], sent=bool(sent),
    )


@router.post("/keys/request")
def request_keys_access(request: Request):
    current_user = require_admin(request)
    service.request_key_access(current_user["user_id"], email_link_base(request))
    return RedirectResponse("/admin/keys?sent=1", status_code=303)


@router.get("/keys/confirm")
def confirm_keys_access(request: Request, token: Annotated[str, Query()] = ""):
    # The emailed token is the credential, like a password-reset link.
    user_id = service.confirm_key_access(token)
    response = RedirectResponse("/admin/keys", status_code=303)
    response.set_cookie(
        core_auth.KEY_ACCESS_COOKIE, core_auth.create_key_access_token(user_id),
        max_age=core_auth.KEY_ACCESS_MAX_AGE_SECONDS, httponly=True, samesite="lax", secure=is_https(request),
    )
    return response


@router.post("/keys/add")
def add_key(request: Request, value: Annotated[str, Form(max_length=200)] = ""):
    current_user = require_admin(request)
    require_key_access(request, current_user["user_id"])
    app_settings.add_key(value, current_user["user_id"])
    return RedirectResponse("/admin/keys", status_code=303)


@router.post("/keys/{index}/edit")
def edit_key(request: Request, index: int, value: Annotated[str, Form(max_length=200)] = ""):
    current_user = require_admin(request)
    require_key_access(request, current_user["user_id"])
    app_settings.replace_key(index, value, current_user["user_id"])
    return RedirectResponse("/admin/keys", status_code=303)


@router.post("/keys/{index}/delete")
def delete_key(request: Request, index: int):
    current_user = require_admin(request)
    require_key_access(request, current_user["user_id"])
    app_settings.delete_key(index, current_user["user_id"])
    return RedirectResponse("/admin/keys", status_code=303)
