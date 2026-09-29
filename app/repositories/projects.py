from uuid import uuid4

from psycopg.rows import dict_row

from app.storage import postgres_connection

FIELDS = "id, name, description, created_at"


def create_project(payload):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"INSERT INTO projects (id, name, description) "
                f"VALUES (%s, %s, %s) RETURNING {FIELDS}",
                (uuid4(), payload.name, payload.description),
            )
            return cursor.fetchone()


def list_projects(limit, offset, query=None):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            if query:
                cursor.execute(
                    f"SELECT {FIELDS} FROM projects WHERE name ILIKE %s "
                    "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                    (f"%{query}%", limit, offset),
                )
            else:
                cursor.execute(
                    f"SELECT {FIELDS} FROM projects "
                    "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                    (limit, offset),
                )
            return cursor.fetchall()


def count_projects(query=None):
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
            if query:
                cursor.execute("SELECT count(*) FROM projects WHERE name ILIKE %s", (f"%{query}%",))
            else:
                cursor.execute("SELECT count(*) FROM projects")
            return cursor.fetchone()[0]


def get_project(project_id):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {FIELDS} FROM projects WHERE id = %s",
                (project_id,),
            )
            return cursor.fetchone()


def update_project(project_id, name, description):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"UPDATE projects SET name = %s, description = %s WHERE id = %s RETURNING {FIELDS}",
                (name, description, project_id),
            )
            return cursor.fetchone()


def delete_project(project_id):
    with postgres_connection() as connection:
        connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))
