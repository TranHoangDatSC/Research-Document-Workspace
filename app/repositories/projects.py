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


def list_projects(limit, offset):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {FIELDS} FROM projects "
                "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            return cursor.fetchall()


def count_projects():
    with postgres_connection() as connection:
        with connection.cursor() as cursor:
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
