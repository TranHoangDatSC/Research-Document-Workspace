"""Accounts: login and session checks, self-service sign-up with email
verification, password reset/change, and admin management. Two roles today
(user, admin); `users.role` is a plain VARCHAR with a CHECK constraint (see
app/bootstrap.py) so a future role only needs an additive constraint change.
"""
import hashlib
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

import psycopg
from fastapi import HTTPException

from app import auth, mailer
from app.branding import APP_NAME
from app.repositories import users as repository

log = logging.getLogger("uvicorn.error")
ROLES = ("user", "admin")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 200
RESET_TOKEN_MINUTES = 60
VERIFY_TOKEN_HOURS = 48
KEY_ACCESS_TOKEN_MINUTES = 15
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SIGNUP_SETTING = "signup_open"

# Verified against when the username doesn't exist, so a wrong username takes
# as long as a wrong password (no telling which usernames exist by timing).
_DUMMY_HASH = auth.hash_password(secrets.token_hex(16))


def _unavailable(stage, exc):
    log.warning("auth_storage_failed stage=%s error=%s", stage, type(exc).__name__)
    return HTTPException(503, "Account storage is temporarily unavailable")


def _public(row):
    return {k: v for k, v in row.items() if k != "password_hash"}


# ----- login and sessions -----

def authenticate(username, password):
    """The account if the password is right and it isn't locked, else None.
    Says nothing about email verification — the caller decides."""
    try:
        row = repository.get_by_username(username)
    except psycopg.Error as exc:
        raise _unavailable("login", exc) from None
    if row is None:
        auth.verify_password(password, _DUMMY_HASH)
        return None
    if not auth.verify_password(password, row["password_hash"]) or not row["is_active"]:
        return None
    return _public(row)


def session_token(user):
    return auth.create_session_token(user["id"], user["username"], user["role"], user["session_version"])


def session_user(session):
    """Checks a signature-valid session against the database on every request:
    the account must still exist, be active, and not have revoked this
    session (session_version). Role and username come from the database, so
    a demoted admin loses admin pages immediately, not when the cookie expires."""
    try:
        row = repository.get_by_id(session["user_id"])
    except psycopg.Error as exc:
        raise _unavailable("session", exc) from None
    if row is None or not row["is_active"] or row["session_version"] != session["session_version"]:
        return None
    return {"user_id": str(row["id"]), "username": row["username"], "role": row["role"]}


def change_password(user_id, current_password, new_password, new_password_confirm):
    """Signed-in change: needs the current password; ends every session
    (the caller re-issues a cookie for the device that made the change)."""
    try:
        row = repository.get_by_id(user_id, with_password=True)
    except psycopg.Error as exc:
        raise _unavailable("change-password", exc) from None
    if row is None or not auth.verify_password(current_password, row["password_hash"]):
        raise HTTPException(422, "Mật khẩu hiện tại không đúng")
    if new_password != new_password_confirm:
        raise HTTPException(422, "Hai lần nhập mật khẩu mới không khớp")
    _check_password(new_password)
    try:
        user = repository.set_password(user_id, auth.hash_password(new_password))
    except psycopg.Error as exc:
        raise _unavailable("change-password", exc) from None
    log.info("password_changed user_id=%s", user_id)
    return user


def logout_everywhere(user_id):
    try:
        repository.bump_session_version(user_id)
    except psycopg.Error as exc:
        raise _unavailable("logout-everywhere", exc) from None
    log.info("sessions_revoked user_id=%s", user_id)


def get_account(user_id):
    try:
        return repository.get_by_id(user_id)
    except psycopg.Error as exc:
        raise _unavailable("account", exc) from None


# ----- validation -----

def _check_password(password):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(422, f"Mật khẩu phải có ít nhất {MIN_PASSWORD_LENGTH} ký tự")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise HTTPException(422, f"Mật khẩu tối đa {MAX_PASSWORD_LENGTH} ký tự")


def normalize_email(email):
    """Trimmed email, or None when blank. 422 when it doesn't look like one —
    the real check is the verification email arriving."""
    email = (email or "").strip()
    if not email:
        return None
    if len(email) > 254 or not EMAIL_PATTERN.match(email):
        raise HTTPException(422, "Email không hợp lệ")
    return email


def create_user(username, password, role, email=None, email_verified=True):
    """Admin-created accounts are verified by default: the admin vouches."""
    if not auth.USERNAME_PATTERN.match(username or ""):
        raise HTTPException(422, "Tên đăng nhập phải có 3–50 ký tự: chữ không dấu, số, dấu chấm, gạch dưới, gạch ngang")
    _check_password(password)
    if role not in ROLES:
        raise HTTPException(422, "Vai trò phải là Người dùng hoặc Quản trị viên")
    email = normalize_email(email)
    try:
        row = repository.create_user(username, auth.hash_password(password), role, email, email_verified)
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name == "users_email_lower_key":
            raise HTTPException(409, "Email này đã được dùng cho một tài khoản khác") from None
        raise HTTPException(409, "Tên đăng nhập đã có người dùng") from None
    except psycopg.Error as exc:
        raise _unavailable("create-user", exc) from None
    log.info("user_created user_id=%s role=%s", row["id"], role)
    return row


# ----- sign-up: closed unless an admin opens it -----

def signup_locked_by_env():
    """ALLOW_SIGNUP=false is a hard switch: sign-up stays closed whatever the admin setting."""
    return os.environ.get("ALLOW_SIGNUP", "true").strip().lower() in ("false", "0", "no", "off")


def signup_enabled():
    """Closed by default (internal tool): open only when an admin turned it on
    and the server's .env doesn't forbid it. A database error reads as closed."""
    if signup_locked_by_env():
        return False
    try:
        return repository.get_setting(SIGNUP_SETTING) == "true"
    except psycopg.Error as exc:
        log.warning("auth_storage_failed stage=signup-setting error=%s", type(exc).__name__)
        return False


def set_signup_open(is_open, admin_id):
    if is_open and signup_locked_by_env():
        raise HTTPException(409, "Máy chủ đặt ALLOW_SIGNUP=false trong .env nên không mở đăng ký được từ giao diện")
    try:
        repository.set_setting(SIGNUP_SETTING, "true" if is_open else "false", admin_id)
    except psycopg.Error as exc:
        raise _unavailable("signup-setting", exc) from None
    log.info("signup_setting_changed open=%s admin_id=%s", is_open, admin_id)


def register(username, email, password, password_confirm, base_url):
    """Public sign-up: role `user`, email required and must be confirmed
    through the emailed link before the account can log in."""
    if not signup_enabled():
        raise HTTPException(403, "Đăng ký tài khoản đang đóng. Liên hệ quản trị viên để được cấp tài khoản.")
    if not (email or "").strip():
        raise HTTPException(422, "Cần nhập email để xác minh tài khoản và lấy lại mật khẩu khi quên")
    if password != password_confirm:
        raise HTTPException(422, "Hai lần nhập mật khẩu không khớp")
    user = create_user(username, password, "user", email, email_verified=False)
    _send_verification(user, base_url)
    return user


# ----- one-time emailed links -----

def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token(user_id, purpose, lifetime):
    token = secrets.token_urlsafe(32)
    repository.create_token(user_id, _token_hash(token), datetime.now(timezone.utc) + lifetime, purpose)
    return token


def dispatch(job):
    """Runs `job` off the request thread. Tests replace this to run it inline."""
    threading.Thread(target=job, daemon=True).start()


def _mail_later(user, subject, body, event):
    """Sent in the background: answering only after a slow SMTP exchange when
    an email matched would reveal, by timing, which emails have accounts."""
    def deliver():
        try:
            mailer.send(user["email"], subject, body)
        except mailer.MailError as exc:
            log.error("%s_mail_failed user_id=%s error=%s", event, user["id"], exc)
    dispatch(deliver)


def _send_verification(user, base_url):
    if base_url is None:
        log.error("verification_mail_skipped reason=APP_BASE_URL-not-set-while-SMTP-configured")
        return
    try:
        token = _new_token(user["id"], "verify", timedelta(hours=VERIFY_TOKEN_HOURS))
    except psycopg.Error as exc:
        raise _unavailable("verify-token", exc) from None
    link = f"{base_url.rstrip('/')}/verify-email?token={token}"
    body = (
        f"Xin chào {user['username']},\n\n"
        f"Cảm ơn bạn đã đăng ký {APP_NAME}. Mở liên kết sau để xác minh email "
        f"và kích hoạt tài khoản (hiệu lực {VERIFY_TOKEN_HOURS} giờ):\n\n{link}\n\n"
        "Nếu bạn không đăng ký, hãy bỏ qua email này.\n"
    )
    _mail_later(user, f"Xác minh email — {APP_NAME}", body, "verification")


def resend_verification(email, base_url):
    """Neutral like the password-reset form: no hint whether the email exists."""
    try:
        email = normalize_email(email)
    except HTTPException:
        return
    if email is None:
        return
    try:
        user = repository.get_by_email(email)
    except psycopg.Error as exc:
        raise _unavailable("verify-resend", exc) from None
    if user is None or not user["is_active"] or user["email_verified"]:
        return
    _send_verification(user, base_url)


def verify_email(token):
    if not token:
        return None
    try:
        user = repository.verify_email(_token_hash(token))
    except psycopg.Error as exc:
        raise _unavailable("verify-email", exc) from None
    if user:
        log.info("email_verified user_id=%s", user["id"])
    return user


def request_password_reset(email, base_url):
    """Emails a one-time link if `email` belongs to an active account. Says
    nothing either way (the caller shows the same message)."""
    try:
        email = normalize_email(email)
    except HTTPException:
        return
    if email is None:
        return
    try:
        user = repository.get_by_email(email)
        if user is None or not user["is_active"]:
            log.info("password_reset_requested matched=false")
            return
        token = _new_token(user["id"], "reset", timedelta(minutes=RESET_TOKEN_MINUTES))
    except psycopg.Error as exc:
        raise _unavailable("reset-request", exc) from None
    link = f"{base_url.rstrip('/')}/reset-password?token={token}"
    body = (
        f"Xin chào {user['username']},\n\n"
        f"Có yêu cầu đặt lại mật khẩu cho tài khoản {APP_NAME} của bạn.\n"
        f"Mở liên kết sau để đặt mật khẩu mới (hiệu lực {RESET_TOKEN_MINUTES} phút, dùng được một lần):\n\n"
        f"{link}\n\n"
        "Nếu bạn không yêu cầu, hãy bỏ qua email này — mật khẩu hiện tại vẫn giữ nguyên.\n"
    )
    _mail_later(user, f"Đặt lại mật khẩu — {APP_NAME}", body, "password_reset")
    log.info("password_reset_requested matched=true user_id=%s", user["id"])


def request_key_access(user_id, base_url):
    """Emails a one-time confirmation link to the account's OWN address
    before /admin/keys unlocks — see app/auth.py's key-access cookie. Unlike
    the public reset-password flow this isn't trying to hide whether an
    account exists (the caller is already a signed-in admin), so failures
    are surfaced for real instead of staying silent."""
    try:
        user = repository.get_by_id(user_id)
    except psycopg.Error as exc:
        raise _unavailable("key-access-request", exc) from None
    if user is None:
        raise HTTPException(404, "Không tìm thấy tài khoản")
    if not user.get("email") or not user["email_verified"]:
        raise HTTPException(409, "Tài khoản cần có email đã xác minh để dùng tính năng này")
    if base_url is None:
        raise HTTPException(503, "Thiếu APP_BASE_URL trong .env, không gửi được email xác thực")
    try:
        token = _new_token(user["id"], "key_access", timedelta(minutes=KEY_ACCESS_TOKEN_MINUTES))
    except psycopg.Error as exc:
        raise _unavailable("key-access-token", exc) from None
    link = f"{base_url.rstrip('/')}/admin/keys/confirm?token={token}"
    body = (
        f"Xin chào {user['username']},\n\n"
        f"Có yêu cầu xem/quản lý API key kỹ thuật của {APP_NAME}.\n"
        f"Mở liên kết sau để xác nhận (hiệu lực {KEY_ACCESS_TOKEN_MINUTES} phút, dùng được một lần):\n\n"
        f"{link}\n\n"
        "Nếu không phải bạn yêu cầu, hãy đổi mật khẩu ngay và kiểm tra lại tài khoản.\n"
    )
    try:
        mailer.send(user["email"], f"Xác thực quản lý API key — {APP_NAME}", body)
    except mailer.MailError as exc:
        log.error("key_access_mail_failed user_id=%s error=%s", user["id"], exc)
        raise HTTPException(503, "Gửi email xác thực thất bại; kiểm tra log máy chủ") from None
    log.info("key_access_requested user_id=%s", user["id"])


def confirm_key_access(token):
    """The user_id a valid key-access link belonged to — the caller sets
    app/auth.py's cookie for it. Raises on an invalid/used/expired link."""
    if not token:
        raise HTTPException(400, "Liên kết không hợp lệ")
    try:
        user_id = repository.claim_key_access(_token_hash(token))
    except psycopg.Error as exc:
        raise _unavailable("key-access-confirm", exc) from None
    if user_id is None:
        raise HTTPException(400, "Liên kết không hợp lệ, đã dùng, hoặc đã hết hạn. Hãy gửi lại yêu cầu.")
    log.info("key_access_confirmed user_id=%s", user_id)
    return user_id


def reset_token_user(token):
    """The account a reset link is for, or None if the link is invalid/used/expired."""
    if not token:
        return None
    try:
        return repository.token_user(_token_hash(token), "reset")
    except psycopg.Error as exc:
        raise _unavailable("reset-check", exc) from None


def reset_password(token, password, password_confirm):
    if password != password_confirm:
        raise HTTPException(422, "Hai lần nhập mật khẩu không khớp")
    _check_password(password)
    try:
        user = repository.reset_password(_token_hash(token or ""), auth.hash_password(password))
    except psycopg.Error as exc:
        raise _unavailable("reset-password", exc) from None
    if user is None:
        raise HTTPException(400, "Liên kết đặt lại mật khẩu không hợp lệ hoặc đã hết hạn. Hãy yêu cầu liên kết mới.")
    log.info("password_reset_done user_id=%s", user["id"])
    return user


# ----- admin -----

def list_users(limit=None, offset=0, query=None):
    try:
        return repository.list_users(limit, offset, query)
    except psycopg.Error as exc:
        raise _unavailable("list-users", exc) from None


def count_users(query=None):
    try:
        return repository.count_users(query)
    except psycopg.Error as exc:
        raise _unavailable("count-users", exc) from None


def user_stats():
    try:
        return repository.user_stats()
    except psycopg.Error as exc:
        raise _unavailable("user-stats", exc) from None


def set_role(user_id, role, current_user_id):
    if role not in ROLES:
        raise HTTPException(422, "Vai trò phải là Người dùng hoặc Quản trị viên")
    if str(user_id) == str(current_user_id):
        raise HTTPException(400, "Không thể tự đổi role của chính mình")
    try:
        row = repository.set_role(user_id, role)
    except psycopg.Error as exc:
        raise _unavailable("set-role", exc) from None
    if row is None:
        raise HTTPException(404, "User not found")
    return row


def set_active(user_id, is_active, current_user_id):
    if str(user_id) == str(current_user_id):
        raise HTTPException(400, "Không thể tự khóa/mở khóa tài khoản của chính mình")
    try:
        row = repository.set_active(user_id, is_active)
    except psycopg.Error as exc:
        raise _unavailable("set-active", exc) from None
    if row is None:
        raise HTTPException(404, "User not found")
    return row


def mark_email_verified(user_id):
    """Admin override, e.g. when the verification email never arrives."""
    try:
        row = repository.set_email_verified(user_id, True)
    except psycopg.Error as exc:
        raise _unavailable("mark-verified", exc) from None
    if row is None:
        raise HTTPException(404, "User not found")
    return row
