"""Projects (PostgreSQL)."""
from uuid import uuid4

from psycopg.rows import dict_row

from app.repositories import escape_like
from app.storage import postgres_connection

FIELDS = "id, name, description, created_at"

# Every query is scoped to the owner: another user's project looks missing.


def create_project(payload, owner_id):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"INSERT INTO projects (id, name, description, owner_id) "
                f"VALUES (%s, %s, %s, %s) RETURNING {FIELDS}",
                (uuid4(), payload.name, payload.description, owner_id),
            )
            return cursor.fetchone()


def list_projects(limit, offset, query=None, owner_id=None):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            if query:
                cursor.execute(
                    f"SELECT {FIELDS} FROM projects WHERE owner_id = %s AND name ILIKE %s ESCAPE '\\' "
                    "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                    (owner_id, f"%{escape_like(query)}%", limit, offset),
                )
            else:
                cursor.execute(
                    f"SELECT {FIELDS} FROM projects WHERE owner_id = %s "
                    "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                    (owner_id, limit, offset),
                )
            return cursor.fetchall()


def count_projects(query=None, owner_id=None):
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            if query:
                cursor.execute(
                    "SELECT count(*) FROM projects WHERE owner_id = %s AND name ILIKE %s ESCAPE '\\'",
                    (owner_id, f"%{escape_like(query)}%"),
                )
            else:
                cursor.execute("SELECT count(*) FROM projects WHERE owner_id = %s", (owner_id,))
            return cursor.fetchone()[0]


def get_project(project_id, owner_id):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {FIELDS} FROM projects WHERE id = %s AND owner_id = %s",
                (project_id, owner_id),
            )
            return cursor.fetchone()


def update_project(project_id, name, description, owner_id):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"UPDATE projects SET name = %s, description = %s WHERE id = %s AND owner_id = %s RETURNING {FIELDS}",
                (name, description, project_id, owner_id),
            )
            return cursor.fetchone()


def delete_project(project_id, owner_id):
    with postgres_connection() as connection:
        connection.execute("DELETE FROM projects WHERE id = %s AND owner_id = %s", (project_id, owner_id))
