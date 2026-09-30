"""Shared test support: an in-memory stand-in for every storage boundary.

`FakeBackend.install(test)` replaces the repository functions for
PostgreSQL (projects, documents, users), MongoDB (document details, chat
history) and the MinIO client with dictionaries, so the real services,
routes, middleware and templates run end to end with no Docker.

Failure injection: add a name to `backend.fail` ("mongo", "minio",
"sql-finish") to make that store raise, then remove it to test the retry.
"""
import io
import os
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-production")

import psycopg
from fastapi.testclient import TestClient

from app import auth
from app.api import health
from app.main import app
from app.repositories import chats as chats_repo
from app.repositories import documents as documents_repo
from app.repositories import projects as projects_repo
from app.repositories import users as users_repo
from app.services import documents as documents_service

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ObjectResponse(io.BytesIO):
    """What minio's get_object returns: read() / stream() / close() / release_conn()."""

    def stream(self, amt):
        while chunk := self.read(amt):
            yield chunk

    def release_conn(self):
        pass


class FakeBackend:
    def __init__(self):
        self.projects = {}   # PostgreSQL projects: id -> row
        self.documents = {}  # PostgreSQL documents: id -> row
        self.users = {}      # PostgreSQL users: id -> row (with password_hash)
        self.details = {}    # MongoDB document_details: str(document_id) -> dict
        self.chats = []      # MongoDB chat_messages
        self.objects = {}    # MinIO: object name -> bytes
        self.content_types = {}  # MinIO: object name -> Content-Type given on upload
        self.downloads = []  # MinIO: object names read, in order
        self.events = []     # order of delete steps, for the retry tests
        self.fail = set()
        self._clock = count()

    def _now(self):
        # Strictly increasing timestamps: "newest first" ordering is deterministic.
        return _BASE_TIME + timedelta(seconds=next(self._clock))

    def _check(self, store):
        if store in self.fail:
            raise RuntimeError(f"simulated {store} failure")

    # ----- PostgreSQL: projects -----
    def create_project(self, payload):
        row = {"id": uuid4(), "name": payload.name, "description": payload.description, "created_at": self._now()}
        self.projects[row["id"]] = row
        return dict(row)

    def _matching_projects(self, query):
        rows = sorted(self.projects.values(), key=lambda r: r["created_at"], reverse=True)
        if query:
            rows = [r for r in rows if query.lower() in r["name"].lower()]
        return rows

    def list_projects(self, limit, offset, query=None):
        return [dict(r) for r in self._matching_projects(query)[offset:offset + limit]]

    def count_projects(self, query=None):
        return len(self._matching_projects(query))

    def get_project(self, project_id):
        row = self.projects.get(project_id)
        return dict(row) if row else None

    def update_project(self, project_id, name, description):
        row = self.projects.get(project_id)
        if row is None:
            return None
        row.update(name=name, description=description)
        return dict(row)

    def delete_project(self, project_id):
        self.projects.pop(project_id, None)

    # ----- PostgreSQL: documents -----
    def project_exists(self, project_id):
        return {"id": project_id} if project_id in self.projects else None

    def get_document(self, document_id):
        row = self.documents.get(document_id)
        return dict(row) if row else None

    def create_pending(self, document_id, project_id, filename, object_name, content_type, size):
        self.documents[document_id] = {
            "id": document_id, "project_id": project_id, "original_name": filename,
            "object_name": object_name, "content_type": content_type, "size_bytes": size,
            "status": "pending", "created_at": self._now(),
        }
        return {"id": document_id}

    def mark_ready(self, document_id):
        self.documents[document_id]["status"] = "ready"
        return dict(self.documents[document_id])

    def mark_failed(self, document_id):
        row = self.documents.get(document_id)
        if row and row["status"] == "pending":
            row["status"] = "failed"

    def list_documents(self, project_id, limit, offset):
        rows = sorted(
            (r for r in self.documents.values() if r["project_id"] == project_id),
            key=lambda r: r["created_at"], reverse=True,
        )
        return [dict(r) for r in rows[offset:offset + limit]]

    def list_all_documents(self, project_id):
        return [dict(r) for r in self.documents.values() if r["project_id"] == project_id]

    def begin_delete(self, document_id):
        self.events.append("intent")
        row = self.documents.get(document_id)
        if row and row["status"] in ("ready", "failed", "deleting"):
            row["status"] = "deleting"
            return dict(row)
        return None

    def finish_delete(self, document_id):
        self.events.append("sql-delete")
        if "sql-finish" in self.fail:
            raise psycopg.OperationalError("simulated sql failure")
        if self.documents.get(document_id, {}).get("status") == "deleting":
            self.documents.pop(document_id)

    # ----- MongoDB: document details -----
    def insert_details(self, details):
        self._check("mongo")
        self.details[details["document_id"]] = dict(details)

    def get_details(self, document_id):
        self._check("mongo")
        found = self.details.get(str(document_id))
        return dict(found) if found else None

    def delete_details(self, document_id):
        self.events.append("mongo-delete")
        self._check("mongo")
        self.details.pop(str(document_id), None)

    def update_extracted_text(self, document_id, extracted):
        self._check("mongo")
        self.details[str(document_id)]["extracted_text"] = extracted

    def update_details(self, document_id, tags, authors, custom_metadata):
        self._check("mongo")
        self.details[str(document_id)].update(tags=tags, authors=authors, custom_metadata=custom_metadata)

    # ----- MongoDB: chat history -----
    def add_messages(self, messages):
        self._check("mongo")
        now = self._now()
        for seq, m in enumerate(messages):
            self.chats.append({**m, "project_id": str(m["project_id"]), "user_id": str(m["user_id"]), "created_at": now, "seq": seq})

    def list_messages(self, project_id, user_id, limit):
        self._check("mongo")
        mine = [
            {k: v for k, v in m.items()}
            for m in self.chats
            if m["project_id"] == str(project_id) and m["user_id"] == str(user_id)
        ]
        return mine[-limit:]

    def clear_chat(self, project_id, user_id):
        self._check("mongo")
        before = len(self.chats)
        self.chats = [m for m in self.chats if not (m["project_id"] == str(project_id) and m["user_id"] == str(user_id))]
        return before - len(self.chats)

    def delete_project_chats(self, project_id):
        self._check("mongo")
        self.chats = [m for m in self.chats if m["project_id"] != str(project_id)]

    # ----- PostgreSQL: users -----
    def seed_user(self, username, password=None, role="user", is_active=True):
        # PBKDF2 is deliberately slow (~0.1 s): only hash when the test logs in
        # with the password; cookie-authenticated clients get an unusable hash.
        row = {
            "id": uuid4(), "username": username, "role": role, "is_active": is_active,
            "created_at": self._now(),
            "password_hash": auth.hash_password(password) if password else "!",
        }
        self.users[row["id"]] = row
        return row

    def _public_user(self, row):
        return {k: v for k, v in row.items() if k != "password_hash"}

    def create_user(self, username, password_hash, role):
        if any(u["username"] == username for u in self.users.values()):
            raise psycopg.errors.UniqueViolation("duplicate username")
        row = {"id": uuid4(), "username": username, "password_hash": password_hash, "role": role, "is_active": True, "created_at": self._now()}
        self.users[row["id"]] = row
        return self._public_user(row)

    def get_by_username(self, username):
        row = next((u for u in self.users.values() if u["username"] == username), None)
        return dict(row) if row else None

    def get_user_by_id(self, user_id):
        row = self.users.get(user_id)
        return self._public_user(row) if row else None

    def list_users(self):
        return [self._public_user(u) for u in sorted(self.users.values(), key=lambda u: u["created_at"])]

    def count_users(self):
        return len(self.users)

    def _update_user(self, user_id, **fields):
        row = self.users.get(user_id)
        if row is None:
            return None
        row.update(fields)
        return self._public_user(row)

    def set_role(self, user_id, role):
        return self._update_user(user_id, role=role)

    def set_active(self, user_id, is_active):
        return self._update_user(user_id, is_active=is_active)

    # ----- MinIO -----
    def put_object(self, bucket, key, stream, size, **kwargs):
        self._check("minio")
        self.objects[key] = stream.read(size)
        self.content_types[key] = kwargs.get("content_type")

    def get_object(self, bucket, key, offset=0, length=0):
        self._check("minio")
        self.downloads.append(key)
        data = self.objects[key]
        return ObjectResponse(data[offset:offset + length] if length else data[offset:])

    def remove_object(self, bucket, key):
        self.events.append("object-delete")
        self._check("minio")
        self.objects.pop(key, None)

    # ----- wiring -----
    def install(self, test):
        replacements = {
            projects_repo: {
                "create_project": self.create_project, "list_projects": self.list_projects,
                "count_projects": self.count_projects, "get_project": self.get_project,
                "update_project": self.update_project, "delete_project": self.delete_project,
            },
            documents_repo: {
                name: getattr(self, name) for name in (
                    "project_exists", "get_document", "create_pending", "mark_ready", "mark_failed",
                    "list_documents", "list_all_documents", "begin_delete", "finish_delete",
                    "insert_details", "get_details", "delete_details", "update_extracted_text", "update_details",
                )
            },
            chats_repo: {
                "add_messages": self.add_messages, "list_messages": self.list_messages,
                "clear": self.clear_chat, "delete_project": self.delete_project_chats,
            },
            users_repo: {
                "create_user": self.create_user, "get_by_username": self.get_by_username,
                "get_by_id": self.get_user_by_id, "list_users": self.list_users,
                "count_users": self.count_users, "set_role": self.set_role, "set_active": self.set_active,
            },
            documents_service: {"minio_client": lambda: self, "bucket_name": lambda: "test"},
            health: {name: (lambda: None) for name in ("check_postgres", "check_mongodb", "check_minio")},
        }
        for module, functions in replacements.items():
            for name, fake in functions.items():
                patcher = patch.object(module, name, fake)
                patcher.start()
                test.addCleanup(patcher.stop)
        return self

    def client(self, test, role="admin", username="tester"):
        """TestClient already logged in (session cookie) as a seeded user.
        role=None gives an anonymous client."""
        client = TestClient(app)
        test.addCleanup(client.close)
        if role is not None:
            user = self.seed_user(username, role=role)
            client.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(user["id"], username, role))
            client.user = self._public_user(user)
        return client


def upload(client, project_id, name="paper.txt", content=b"original bytes", tags="cloud,docker", authors="Dat", custom_metadata='{"year": 2026}'):
    """Uploads through the browser form; returns the created document (JSON API view)."""
    response = client.post(
        f"/ui/projects/{project_id}/documents",
        files={"file": (name, content, "text/plain")},
        data={"tags": tags, "authors": authors, "custom_metadata": custom_metadata},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    document_id = response.headers["location"].rsplit("/", 1)[-1]
    return client.get(f"/documents/{document_id}").json()
