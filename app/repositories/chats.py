"""Chat history in MongoDB (`chat_messages`): one running conversation per
(project, user). Flexible, append-only records with a nested `sources` list —
the same reason document metadata lives in MongoDB rather than PostgreSQL.
"""
import os
from datetime import datetime, timezone

from app.storage import mongo_client

COLLECTION = "chat_messages"


def _collection(client):
    return client[os.environ["MONGO_DB"]][COLLECTION]


def add_messages(messages):
    """messages: [{project_id, user_id, role, content, sources?, model?}], stored
    with one shared timestamp plus a sequence so a question always sorts before
    its answer."""
    now = datetime.now(timezone.utc)
    records = [
        {**m, "project_id": str(m["project_id"]), "user_id": str(m["user_id"]), "created_at": now, "seq": i}
        for i, m in enumerate(messages)
    ]
    with mongo_client() as client:
        _collection(client).insert_many(records)


def list_messages(project_id, user_id, limit):
    """The most recent `limit` messages, oldest first."""
    with mongo_client() as client:
        cursor = (
            _collection(client)
            .find({"project_id": str(project_id), "user_id": str(user_id)}, {"_id": 0})
            .sort([("created_at", -1), ("seq", -1)])
            .limit(limit)
        )
        return list(reversed(list(cursor)))


def clear(project_id, user_id):
    with mongo_client() as client:
        return _collection(client).delete_many(
            {"project_id": str(project_id), "user_id": str(user_id)}
        ).deleted_count


def delete_project(project_id):
    with mongo_client() as client:
        _collection(client).delete_many({"project_id": str(project_id)})
