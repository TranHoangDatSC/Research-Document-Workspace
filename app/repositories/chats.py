"""Chat history in MongoDB (`chat_messages`): a project can hold several
conversation threads per user, told apart by `chat_id`. Flexible, append-only
records with a nested `sources` list — the same reason document metadata
lives in MongoDB rather than PostgreSQL.
"""
import os
from datetime import datetime, timezone

from app.storage import mongo_client

COLLECTION = "chat_messages"
# list_chats only needs to tell threads apart and preview each one — scanning
# the most recent few hundred messages is enough for what's meant to be a
# handful of threads per project, and avoids a Mongo aggregation pipeline
# (nothing else in this codebase uses one) for something this small.
THREAD_SCAN_LIMIT = 500


def _collection(client):
    return client[os.environ["MONGO_DB"]][COLLECTION]


def add_messages(messages):
    """messages: [{project_id, user_id, chat_id, role, content, sources?, model?}],
    stored with one shared timestamp plus a sequence so a question always
    sorts before its answer."""
    now = datetime.now(timezone.utc)
    records = [
        {
            **m, "project_id": str(m["project_id"]), "user_id": str(m["user_id"]),
            "chat_id": str(m["chat_id"]), "created_at": now, "seq": i,
        }
        for i, m in enumerate(messages)
    ]
    with mongo_client() as client:
        _collection(client).insert_many(records)


def list_messages(project_id, user_id, chat_id, limit):
    """The most recent `limit` messages of one thread, oldest first."""
    with mongo_client() as client:
        cursor = (
            _collection(client)
            .find({"project_id": str(project_id), "user_id": str(user_id), "chat_id": str(chat_id)}, {"_id": 0})
            .sort([("created_at", -1), ("seq", -1)])
            .limit(limit)
        )
        return list(reversed(list(cursor)))


def list_chats(project_id, user_id):
    """Every distinct thread for (project, user), most-recently-used first,
    each with a short preview (its first question)."""
    with mongo_client() as client:
        cursor = (
            _collection(client)
            .find({"project_id": str(project_id), "user_id": str(user_id)}, {"_id": 0})
            .sort([("created_at", -1), ("seq", -1)])
            .limit(THREAD_SCAN_LIMIT)
        )
        threads = {}
        for m in cursor:  # newest message first
            chat_id = m.get("chat_id")
            if not chat_id:
                continue
            entry = threads.setdefault(chat_id, {"chat_id": chat_id, "preview": None, "updated_at": m["created_at"]})
            if m.get("role") == "user":
                # Overwritten on every older user message of this thread we
                # visit next; the last write (the thread's first question)
                # is what's left once the scan finishes.
                entry["preview"] = m["content"][:140]
        return sorted(threads.values(), key=lambda t: t["updated_at"], reverse=True)


def delete_chat(project_id, user_id, chat_id):
    with mongo_client() as client:
        return _collection(client).delete_many(
            {"project_id": str(project_id), "user_id": str(user_id), "chat_id": str(chat_id)}
        ).deleted_count


def delete_project(project_id):
    with mongo_client() as client:
        _collection(client).delete_many({"project_id": str(project_id)})
