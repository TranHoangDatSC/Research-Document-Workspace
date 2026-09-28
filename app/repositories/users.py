from uuid import uuid4

from psycopg.rows import dict_row

from app.storage import postgres_connection

FIELDS = "id, username, role, is_active, created_at"


def create_user(username, password_hash, role):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"INSERT INTO users (id, username, password_hash, role) "
                f"VALUES (%s, %s, %s, %s) RETURNING {FIELDS}",
                (uuid4(), username, password_hash, role),
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


def get_by_id(user_id):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"SELECT {FIELDS} FROM users WHERE id = %s", (user_id,))
            return cursor.fetchone()


def list_users():
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"SELECT {FIELDS} FROM users ORDER BY created_at")
            return cursor.fetchall()


def count_users():
    with postgres_connection() as connection:
        return connection.execute("SELECT count(*) FROM users").fetchone()[0]


def set_role(user_id, role):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"UPDATE users SET role = %s WHERE id = %s RETURNING {FIELDS}",
                (role, user_id),
            )
            return cursor.fetchone()


def set_active(user_id, is_active):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"UPDATE users SET is_active = %s WHERE id = %s RETURNING {FIELDS}",
                (is_active, user_id),
            )
            return cursor.fetchone()
