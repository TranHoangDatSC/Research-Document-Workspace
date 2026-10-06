"""Accounts, one-time tokens and app settings (PostgreSQL)."""
from uuid import uuid4

from psycopg.rows import dict_row

from app.repositories import escape_like
from app.storage import postgres_connection

FIELDS = "id, username, email, email_verified, role, is_active, session_version, created_at"
_U_FIELDS = ", ".join("u." + f.strip() for f in FIELDS.split(","))

# Every change that must end existing logins bumps session_version: cookies
# carry the version they were issued with (app/auth.py) and are refused once
# it no longer matches (services.auth.session_user).


def create_user(username, password_hash, role, email=None, email_verified=False):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"INSERT INTO users (id, username, password_hash, role, email, email_verified) "
                f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {FIELDS}",
                (uuid4(), username, password_hash, role, email, email_verified),
            )
            return cursor.fetchone()


def get_by_username(username):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {FIELDS}, password_hash FROM users WHERE username = %s",
                (username,),
            )
            return cursor.fetchone()


def get_by_email(email):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"SELECT {FIELDS} FROM users WHERE lower(email) = lower(%s)", (email,))
            return cursor.fetchone()


def get_by_id(user_id, with_password=False):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            extra = ", password_hash" if with_password else ""
            cursor.execute(f"SELECT {FIELDS}{extra} FROM users WHERE id = %s", (user_id,))
            return cursor.fetchone()


def list_users(limit=None, offset=0, query=None):
    """`limit=None` returns every user; the admin page paginates."""
    where, params = [], []
    if query:
        where.append("username ILIKE %s ESCAPE '\\'")
        params.append(f"%{escape_like(query)}%")
    clause = f"WHERE {' AND '.join(where)} " if where else ""
    sql = f"SELECT {FIELDS} FROM users {clause}ORDER BY created_at"
    if limit is not None:
        sql += " LIMIT %s OFFSET %s"
        params += [limit, offset]
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()


def count_users(query=None):
    where, params = [], []
    if query:
        where.append("username ILIKE %s ESCAPE '\\'")
        params.append(f"%{escape_like(query)}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with postgres_connection() as connection:
        return connection.execute(f"SELECT count(*) FROM users {clause}", params).fetchone()[0]


def user_stats():
    """Totals over all accounts (not the current page/search)."""
    with postgres_connection() as connection:
        row = connection.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_active), count(*) FILTER (WHERE role = 'admin') FROM users"
        ).fetchone()
        return {"total": row[0], "active": row[1], "admins": row[2]}


def set_role(user_id, role):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"UPDATE users SET role = %s, session_version = session_version + 1 WHERE id = %s RETURNING {FIELDS}",
                (role, user_id),
            )
            return cursor.fetchone()


def set_active(user_id, is_active):
    """Locking also ends the account's open sessions; unlocking doesn't need to."""
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"UPDATE users SET is_active = %s, "
                f"session_version = session_version + CASE WHEN %s THEN 0 ELSE 1 END "
                f"WHERE id = %s RETURNING {FIELDS}",
                (is_active, is_active, user_id),
            )
            return cursor.fetchone()


def set_password(user_id, password_hash):
    """Signed-in password change: ends every other session too."""
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"UPDATE users SET password_hash = %s, session_version = session_version + 1 "
                f"WHERE id = %s RETURNING {FIELDS}",
                (password_hash, user_id),
            )
            return cursor.fetchone()


def bump_session_version(user_id):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"UPDATE users SET session_version = session_version + 1 WHERE id = %s RETURNING {FIELDS}",
                (user_id,),
            )
            return cursor.fetchone()


def set_email_verified(user_id, verified=True):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"UPDATE users SET email_verified = %s WHERE id = %s RETURNING {FIELDS}",
                (verified, user_id),
            )
            return cursor.fetchone()


# ----- one-time account tokens (table password_reset_tokens, purpose 'reset' | 'verify') -----

def create_token(user_id, token_hash, expires_at, purpose):
    with postgres_connection() as connection:
        connection.execute(
            "INSERT INTO password_reset_tokens (token_hash, user_id, expires_at, purpose) VALUES (%s, %s, %s, %s)",
            (token_hash, user_id, expires_at, purpose),
        )


def token_user(token_hash, purpose):
    """The active account a valid (unused, unexpired) token belongs to, else None."""
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {_U_FIELDS} FROM password_reset_tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash = %s AND t.purpose = %s AND t.used_at IS NULL "
                "AND t.expires_at > now() AND u.is_active",
                (token_hash, purpose),
            )
            return cursor.fetchone()


def _claim_token(cursor, token_hash, purpose):
    """Locks the token row (two concurrent uses can't both win); returns user_id or None."""
    cursor.execute(
        "SELECT t.user_id FROM password_reset_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = %s AND t.purpose = %s AND t.used_at IS NULL "
        "AND t.expires_at > now() AND u.is_active FOR UPDATE OF t",
        (token_hash, purpose),
    )
    found = cursor.fetchone()
    return found["user_id"] if found else None


def _burn_tokens(cursor, user_id, purpose):
    cursor.execute(
        "UPDATE password_reset_tokens SET used_at = now() WHERE user_id = %s AND purpose = %s AND used_at IS NULL",
        (user_id, purpose),
    )


def reset_password(token_hash, password_hash):
    """One transaction: claim the reset token, set the password, end every
    session, and burn all open reset links. Following an emailed link also
    proves the address works, so it counts as email verification.
    Returns the user, or None if the token is unknown/used/expired."""
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            user_id = _claim_token(cursor, token_hash, "reset")
            if user_id is None:
                return None
            cursor.execute(
                f"UPDATE users SET password_hash = %s, email_verified = true, "
                f"session_version = session_version + 1 WHERE id = %s RETURNING {FIELDS}",
                (password_hash, user_id),
            )
            user = cursor.fetchone()
            _burn_tokens(cursor, user_id, "reset")
            return user


def verify_email(token_hash):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            user_id = _claim_token(cursor, token_hash, "verify")
            if user_id is None:
                return None
            cursor.execute(f"UPDATE users SET email_verified = true WHERE id = %s RETURNING {FIELDS}", (user_id,))
            user = cursor.fetchone()
            _burn_tokens(cursor, user_id, "verify")
            return user


def claim_key_access(token_hash):
    """One-time confirm for /admin/keys: claims the token, no side effect on
    the account otherwise. Returns the user_id, or None if invalid/used/expired."""
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            user_id = _claim_token(cursor, token_hash, "key_access")
            if user_id is None:
                return None
            _burn_tokens(cursor, user_id, "key_access")
            return user_id


# ----- app-wide settings changed at runtime by admins (table app_settings) -----

def get_setting(key):
    with postgres_connection() as connection:
        row = connection.execute("SELECT value FROM app_settings WHERE key = %s", (key,)).fetchone()
        return row[0] if row else None


def set_setting(key, value, updated_by):
    with postgres_connection() as connection:
        connection.execute(
            "INSERT INTO app_settings (key, value, updated_by, updated_at) VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()",
            (key, value, updated_by),
        )
