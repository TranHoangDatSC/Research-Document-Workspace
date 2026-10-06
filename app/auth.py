"""Password hashing and session-cookie signing. No database access here."""

import hashlib
import hmac
import os
import re
import time

SESSION_COOKIE = "rdw_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600
PBKDF2_ITERATIONS = 200_000
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{3,50}$")

# Separate short-lived cookie for /admin/keys, set after the admin opens a
# one-time emailed link: a stolen session cookie alone can't reach API keys.
KEY_ACCESS_COOKIE = "rdw_key_access"
KEY_ACCESS_MAX_AGE_SECONDS = 15 * 60


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex() + ":" + digest.hex()


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, digest_hex = stored_hash.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def _session_secret() -> bytes:
    return os.environ["SESSION_SECRET"].encode("utf-8")


def create_session_token(user_id, username: str, role: str, session_version: int = 0) -> str:
    """Signed, stateless cookie: user_id:username:role:version:expiry:hmac.
    Bumping users.session_version in the database revokes older cookies
    (checked per request by services.auth.session_user)."""
    # USERNAME_PATTERN has no ':', so the payload splits back unambiguously.
    expires_at = int(time.time()) + SESSION_MAX_AGE_SECONDS
    payload = f"{user_id}:{username}:{role}:{int(session_version)}:{expires_at}"
    signature = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_session_token(token: str):
    """Checks signature and expiry only; the database check is done by
    services.auth.session_user."""
    if not token:
        return None
    parts = token.split(":")
    if len(parts) != 6:
        return None
    user_id, username, role, version, expires_at, signature = parts
    payload = f"{user_id}:{username}:{role}:{version}:{expires_at}"
    expected = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        if int(expires_at) < time.time():
            return None
        version = int(version)
    except ValueError:
        return None
    return {"user_id": user_id, "username": username, "role": role, "session_version": version}


def create_key_access_token(user_id) -> str:
    """Signed key-access cookie value, independent of the session cookie."""
    expires_at = int(time.time()) + KEY_ACCESS_MAX_AGE_SECONDS
    payload = f"{user_id}:{expires_at}"
    signature = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_key_access_token(token: str, user_id) -> bool:
    if not token:
        return False
    parts = token.split(":")
    if len(parts) != 3:
        return False
    token_user_id, expires_at, signature = parts
    payload = f"{token_user_id}:{expires_at}"
    expected = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature) or token_user_id != str(user_id):
        return False
    try:
        return int(expires_at) >= time.time()
    except ValueError:
        return False
