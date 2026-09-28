import os

from psycopg.rows import dict_row

from app.storage import postgres_connection, mongo_client

FIELDS = "id, project_id, original_name, object_name, content_type, size_bytes, status, created_at"


def sql_one(statement, params):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(statement, params)
            return cursor.fetchone()


def project_exists(project_id):
    return sql_one("SELECT id FROM projects WHERE id = %s", (project_id,))


def get_document(document_id):
    return sql_one(f"SELECT {FIELDS} FROM documents WHERE id = %s", (document_id,))


def create_pending(document_id, project_id, filename, object_name, content_type, size):
    return sql_one(
        "INSERT INTO documents "
        "(id, project_id, original_name, object_name, content_type, size_bytes, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'pending') RETURNING id",
        (document_id, project_id, filename, object_name, content_type, size),
    )


def mark_ready(document_id):
    return sql_one(
        f"UPDATE documents SET status = 'ready' WHERE id = %s RETURNING {FIELDS}",
        (document_id,),
    )


def mark_failed(document_id):
    with postgres_connection() as connection:
        connection.execute(
            "UPDATE documents SET status = 'failed' WHERE id = %s AND status = 'pending'",
            (document_id,),
        )


def list_documents(project_id, limit, offset):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {FIELDS} FROM documents WHERE project_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (project_id, limit, offset),
            )
            return cursor.fetchall()


def insert_details(details):
    with mongo_client() as client:
        client[os.environ["MONGO_DB"]]["document_details"].insert_one(details.copy())


def delete_details(document_id):
    with mongo_client() as client:
        client[os.environ["MONGO_DB"]]["document_details"].delete_one(
            {"document_id": str(document_id)}
        )


def get_details(document_id):
    with mongo_client() as client:
        return client[os.environ["MONGO_DB"]]["document_details"].find_one(
            {"document_id": str(document_id)}, {"_id": 0}
        )


def update_extracted_text(document_id, extracted):
    with mongo_client() as client:
        client[os.environ["MONGO_DB"]]["document_details"].update_one(
            {"document_id": str(document_id)}, {"$set": {"extracted_text": extracted}}
        )


def begin_delete(document_id):
    # One atomic UPDATE commits intent before any external deletion. Pending
    # uploads cannot be deleted while their writer is still finishing.
    return sql_one(
        f"UPDATE documents SET status='deleting' "
        f"WHERE id=%s AND status IN ('ready', 'failed', 'deleting') RETURNING {FIELDS}",
        (document_id,),
    )


def finish_delete(document_id):
    with postgres_connection() as connection:
        connection.execute("DELETE FROM documents WHERE id=%s AND status='deleting'", (document_id,))
