"""Login and admin-managed user accounts. Two roles today (user, admin);
`users.role` is a plain VARCHAR with a CHECK constraint (see app/bootstrap.py)
so a future role only needs an additive constraint change, the same pattern
Day 3 used to add the `deleting` document status.
"""
import logging

import psycopg
from fastapi import HTTPException

from app import auth
from app.repositories import users as repository

log = logging.getLogger("uvicorn.error")
ROLES = ("user", "admin")
MIN_PASSWORD_LENGTH = 8


def _public(row):
    return {k: v for k, v in row.items() if k != "password_hash"}


def authenticate(username, password):
    try:
        row = repository.get_by_username(username)
    except psycopg.Error as exc:
        log.warning("auth_storage_failed stage=login error=%s", type(exc).__name__)
        raise HTTPException(503, "Account storage is temporarily unavailable") from None
    if row is None or not row["is_active"]:
        return None
    if not auth.verify_password(password, row["password_hash"]):
        return None
    return _public(row)


def create_user(username, password, role):
    if not auth.USERNAME_PATTERN.match(username or ""):
        raise HTTPException(422, "Username phải 3-50 ký tự: chữ, số, dấu chấm, gạch dưới, gạch ngang")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(422, f"Mật khẩu phải có ít nhất {MIN_PASSWORD_LENGTH} ký tự")
    if role not in ROLES:
        raise HTTPException(422, "Role phải là 'user' hoặc 'admin'")
    try:
        row = repository.create_user(username, auth.hash_password(password), role)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "Username đã tồn tại") from None
    except psycopg.Error as exc:
        log.warning("auth_storage_failed stage=create-user error=%s", type(exc).__name__)
        raise HTTPException(503, "Account storage is temporarily unavailable") from None
    log.info("user_created user_id=%s role=%s", row["id"], role)
    return row


def list_users():
    try:
        return repository.list_users()
    except psycopg.Error as exc:
        log.warning("auth_storage_failed stage=list-users error=%s", type(exc).__name__)
        raise HTTPException(503, "Account storage is temporarily unavailable") from None


def set_role(user_id, role, current_user_id):
    if role not in ROLES:
        raise HTTPException(422, "Role phải là 'user' hoặc 'admin'")
    if str(user_id) == str(current_user_id):
        raise HTTPException(400, "Không thể tự đổi role của chính mình")
    try:
        row = repository.set_role(user_id, role)
    except psycopg.Error as exc:
        log.warning("auth_storage_failed stage=set-role error=%s", type(exc).__name__)
        raise HTTPException(503, "Account storage is temporarily unavailable") from None
    if row is None:
        raise HTTPException(404, "User not found")
    return row


def set_active(user_id, is_active, current_user_id):
    if str(user_id) == str(current_user_id):
        raise HTTPException(400, "Không thể tự khóa/mở khóa tài khoản của chính mình")
    try:
        row = repository.set_active(user_id, is_active)
    except psycopg.Error as exc:
        log.warning("auth_storage_failed stage=set-active error=%s", type(exc).__name__)
        raise HTTPException(503, "Account storage is temporarily unavailable") from None
    if row is None:
        raise HTTPException(404, "User not found")
    return row
