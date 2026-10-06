"""Document storage: fixed fields in PostgreSQL (`documents`), flexible
metadata, extracted text and entity graph in MongoDB (`document_details`)."""
import os

from psycopg.rows import dict_row

from app.repositories import escape_like
from app.storage import postgres_connection, mongo_client

FIELDS = "id, project_id, original_name, object_name, content_type, size_bytes, status, created_at"


def sql_one(statement, params):
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(statement, params)
            return cursor.fetchone()


def project_exists(project_id, owner_id):
    """Only the owner's projects "exist" — another user's id reads as missing."""
    return sql_one("SELECT id FROM projects WHERE id = %s AND owner_id = %s", (project_id, owner_id))


def get_document(document_id):
    """Unscoped: only for internal steps after ownership was already checked."""
    return sql_one(f"SELECT {FIELDS} FROM documents WHERE id = %s", (document_id,))


def get_owned_document(document_id, owner_id):
    return sql_one(
        f"SELECT {', '.join('d.' + f.strip() for f in FIELDS.split(','))} FROM documents d "
        "JOIN projects p ON p.id = d.project_id WHERE d.id = %s AND p.owner_id = %s",
        (document_id, owner_id),
    )


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


def _document_filters(project_id, query, extensions):
    where, params = ["project_id = %s"], [project_id]
    if query:
        where.append("original_name ILIKE %s ESCAPE '\\'")
        params.append(f"%{escape_like(query)}%")
    if extensions:
        where.append("object_name LIKE ANY(%s)")
        params.append([f"%{ext}" for ext in extensions])
    return where, params


def list_documents(project_id, limit, offset, query=None, extensions=None):
    """`query`: case-insensitive substring of the file name (wildcards in it are
    literal). `extensions`: only files ending in one of these, e.g. [".png", ".jpg"]."""
    where, params = _document_filters(project_id, query, extensions)
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"SELECT {FIELDS} FROM documents WHERE {' AND '.join(where)} "
                "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (*params, limit, offset),
            )
            return cursor.fetchall()


def count_documents(project_id, query=None, extensions=None):
    """Same filters as list_documents, for the sources sidebar's pager."""
    where, params = _document_filters(project_id, query, extensions)
    with postgres_connection() as connection:
        return connection.execute(f"SELECT count(*) FROM documents WHERE {' AND '.join(where)}", params).fetchone()[0]


def extension_totals(project_id):
    """[{"extension": ".pdf", "count": 3, "size_bytes": 1234}] over the whole
    project (every page), ready documents only — for the project overview."""
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT substring(object_name from '\\.[^./]*$') AS extension, "
                "count(*) AS count, coalesce(sum(size_bytes), 0) AS size_bytes "
                "FROM documents WHERE project_id = %s AND status = 'ready' GROUP BY 1",
                (project_id,),
            )
            return cursor.fetchall()


def list_all_documents(project_id):
    """Every document row for a project, no pagination — used to cascade-delete a project."""
    with postgres_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(f"SELECT {FIELDS} FROM documents WHERE project_id = %s", (project_id,))
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


def update_entity_graph(document_id, graph):
    with mongo_client() as client:
        client[os.environ["MONGO_DB"]]["document_details"].update_one(
            {"document_id": str(document_id)}, {"$set": {"entity_graph": graph}}
        )


def update_details(document_id, tags, authors, custom_metadata):
    with mongo_client() as client:
        client[os.environ["MONGO_DB"]]["document_details"].update_one(
            {"document_id": str(document_id)},
            {"$set": {"tags": tags, "authors": authors, "custom_metadata": custom_metadata}},
        )


def begin_delete(document_id):
    # Records the intent to delete before touching MinIO/MongoDB. Pending
    # uploads are excluded: their writer is still running.
    return sql_one(
        f"UPDATE documents SET status='deleting' "
        f"WHERE id=%s AND status IN ('ready', 'failed', 'deleting') RETURNING {FIELDS}",
        (document_id,),
    )


def finish_delete(document_id):
    with postgres_connection() as connection:
        connection.execute("DELETE FROM documents WHERE id=%s AND status='deleting'", (document_id,))


def count_with_text(document_ids):
    """How many of these documents have non-empty extracted text in MongoDB."""
    with mongo_client() as client:
        return client[os.environ["MONGO_DB"]]["document_details"].count_documents({
            "document_id": {"$in": [str(i) for i in document_ids]},
            "extracted_text.text": {"$nin": [None, ""]},
        })
