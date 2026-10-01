from uuid import uuid4

from psycopg.rows import dict_row

from app.storage import postgres_connection

FIELDS = "id, name, description, created_at"

# Every read is scoped to the owner: a project of another user behaves exactly
# like a project that doesn't exist (404), so ids can't be probed.


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
                    f"SELECT {FIELDS} FROM projects WHERE owner_id = %s AND name ILIKE %s "
                    "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                    (owner_id, f"%{query}%", limit, offset),
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
                cursor.execute("SELECT count(*) FROM projects WHERE owner_id = %s AND name ILIKE %s", (owner_id, f"%{query}%"))
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
