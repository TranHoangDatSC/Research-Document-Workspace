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
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-production")

import psycopg
from fastapi.testclient import TestClient

from app import auth, cache, jobs, mailer, ratelimit
from app.api import health
from app.main import app
from app.repositories import chats as chats_repo
from app.repositories import documents as documents_repo
from app.repositories import projects as projects_repo
from app.repositories import usage as usage_repo
from app.repositories import users as users_repo
from app.services import auth as auth_service
from app.services import documents as documents_service

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class UniqueViolation(psycopg.errors.UniqueViolation):
    """Like PostgreSQL's, including which constraint was hit (diag.constraint_name)."""

    def __init__(self, constraint):
        super().__init__(f"duplicate key value violates unique constraint {constraint}")
        self.constraint = constraint

    @property
    def diag(self):
        return SimpleNamespace(constraint_name=self.constraint)


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
        self.usage_log = []  # MongoDB llm_usage
        self.tokens = {}     # PostgreSQL password_reset_tokens (reset + verify): hash -> row
        self.settings = {}   # PostgreSQL app_settings
        self.mail = []       # emails "sent": (to, subject, body)
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

    # ----- PostgreSQL: projects (scoped to owner_id like the real queries) -----
    def create_project(self, payload, owner_id):
        row = {"id": uuid4(), "name": payload.name, "description": payload.description, "created_at": self._now(), "owner_id": str(owner_id)}
        self.projects[row["id"]] = row
        return self._public_project(row)

    @staticmethod
    def _public_project(row):
        return {k: v for k, v in row.items() if k != "owner_id"}

    def _owned(self, project_id, owner_id):
        row = self.projects.get(project_id)
        return row if row and row["owner_id"] == str(owner_id) else None

    def _matching_projects(self, query, owner_id):
        rows = sorted((r for r in self.projects.values() if r["owner_id"] == str(owner_id)), key=lambda r: r["created_at"], reverse=True)
        if query:
            rows = [r for r in rows if query.lower() in r["name"].lower()]
        return rows

    def list_projects(self, limit, offset, query=None, owner_id=None):
        return [self._public_project(r) for r in self._matching_projects(query, owner_id)[offset:offset + limit]]

    def count_projects(self, query=None, owner_id=None):
        return len(self._matching_projects(query, owner_id))

    def get_project(self, project_id, owner_id):
        row = self._owned(project_id, owner_id)
        return self._public_project(row) if row else None

    def update_project(self, project_id, name, description, owner_id):
        row = self._owned(project_id, owner_id)
        if row is None:
            return None
        row.update(name=name, description=description)
        return self._public_project(row)

    def delete_project(self, project_id, owner_id):
        if self._owned(project_id, owner_id):
            self.projects.pop(project_id)

    # ----- PostgreSQL: documents -----
    def project_exists(self, project_id, owner_id):
        return {"id": project_id} if self._owned(project_id, owner_id) else None

    def get_document(self, document_id):
        row = self.documents.get(document_id)
        return dict(row) if row else None

    def get_owned_document(self, document_id, owner_id):
        row = self.documents.get(document_id)
        return dict(row) if row and self._owned(row["project_id"], owner_id) else None

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

    def _matching_documents(self, project_id, query, extensions):
        return [
            r for r in self.documents.values()
            if r["project_id"] == project_id
            and (not query or query.lower() in r["original_name"].lower())
            and (not extensions or r["object_name"].endswith(tuple(extensions)))
        ]

    def list_documents(self, project_id, limit, offset, query=None, extensions=None):
        rows = sorted(self._matching_documents(project_id, query, extensions), key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[offset:offset + limit]]

    def count_documents(self, project_id, query=None, extensions=None):
        return len(self._matching_documents(project_id, query, extensions))

    def extension_totals(self, project_id):
        totals = {}
        for r in self.documents.values():
            if r["project_id"] != project_id or r["status"] != "ready":
                continue
            ext = r["object_name"][r["object_name"].rfind("."):]
            entry = totals.setdefault(ext, {"extension": ext, "count": 0, "size_bytes": 0})
            entry["count"] += 1
            entry["size_bytes"] += r["size_bytes"]
        return list(totals.values())

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

    def update_ai_job(self, document_id, job):
        self._check("mongo")
        if job is None:
            self.details[str(document_id)].pop("ai_job", None)
        else:
            self.details[str(document_id)]["ai_job"] = job

    def update_entity_graph(self, document_id, graph):
        self._check("mongo")
        self.details[str(document_id)]["entity_graph"] = graph

    def count_with_text(self, document_ids):
        self._check("mongo")
        ids = {str(i) for i in document_ids}
        return sum(1 for d in self.details.values() if d["document_id"] in ids and ((d.get("extracted_text") or {}).get("text") or ""))

    def update_details(self, document_id, tags, authors, custom_metadata):
        self._check("mongo")
        self.details[str(document_id)].update(tags=tags, authors=authors, custom_metadata=custom_metadata)

    # ----- MongoDB: chat history (one project/user can hold several threads, told apart by chat_id) -----
    def add_messages(self, messages):
        self._check("mongo")
        now = self._now()
        for seq, m in enumerate(messages):
            self.chats.append({
                **m, "project_id": str(m["project_id"]), "user_id": str(m["user_id"]),
                "chat_id": str(m["chat_id"]), "created_at": now, "seq": seq,
            })

    def list_messages(self, project_id, user_id, chat_id, limit):
        self._check("mongo")
        mine = [
            {k: v for k, v in m.items()}
            for m in self.chats
            if m["project_id"] == str(project_id) and m["user_id"] == str(user_id) and m["chat_id"] == str(chat_id)
        ]
        return mine[-limit:]

    def list_chats(self, project_id, user_id):
        self._check("mongo")
        mine = [
            m for m in self.chats
            if m["project_id"] == str(project_id) and m["user_id"] == str(user_id)
        ]
        threads = {}
        for m in mine:  # stored in insertion order = chronological
            entry = threads.setdefault(m["chat_id"], {"chat_id": m["chat_id"], "preview": None, "updated_at": m["created_at"]})
            entry["updated_at"] = m["created_at"]
            if entry["preview"] is None and m.get("role") == "user":
                entry["preview"] = m["content"][:140]
        return sorted(threads.values(), key=lambda t: t["updated_at"], reverse=True)

    def delete_chat(self, project_id, user_id, chat_id):
        self._check("mongo")
        before = len(self.chats)
        self.chats = [
            m for m in self.chats
            if not (m["project_id"] == str(project_id) and m["user_id"] == str(user_id) and m["chat_id"] == str(chat_id))
        ]
        return before - len(self.chats)

    def delete_project_chats(self, project_id):
        self._check("mongo")
        self.chats = [m for m in self.chats if m["project_id"] != str(project_id)]

    # ----- MongoDB: llm_usage (app/repositories/usage.py) -----
    def record_llm_usage(self, source, provider, model, ok, latency_ms, usage=None, error=None):
        self._check("mongo")
        self.usage_log.append({
            "created_at": self._now(), "source": source, "provider": provider, "model": model,
            "ok": bool(ok), "latency_ms": latency_ms,
            "input_tokens": (usage or {}).get("input"), "output_tokens": (usage or {}).get("output"),
            "thinking_tokens": (usage or {}).get("thinking"), "cached_tokens": (usage or {}).get("cached"),
            "error": error,
        })

    def list_llm_usage_since(self, since):
        self._check("mongo")
        return [r for r in self.usage_log if r["created_at"] >= since]

    # ----- PostgreSQL: users -----
    def seed_user(self, username, password=None, role="user", is_active=True, email=None, email_verified=True):
        # PBKDF2 is deliberately slow (~0.1 s): only hash when the test logs in
        # with the password; cookie-authenticated clients get an unusable hash.
        row = {
            "id": uuid4(), "username": username, "email": email, "email_verified": email_verified,
            "role": role, "is_active": is_active, "session_version": 0, "created_at": self._now(),
            "password_hash": auth.hash_password(password) if password else "!",
        }
        self.users[row["id"]] = row
        return row

    def _public_user(self, row):
        return {k: v for k, v in row.items() if k != "password_hash"}

    def _user(self, user_id):
        return next((u for u in self.users.values() if str(u["id"]) == str(user_id)), None)

    def create_user(self, username, password_hash, role, email=None, email_verified=False):
        if any(u["username"] == username for u in self.users.values()):
            raise UniqueViolation("users_username_key")
        if email and any((u.get("email") or "").lower() == email.lower() for u in self.users.values()):
            raise UniqueViolation("users_email_lower_key")
        row = {"id": uuid4(), "username": username, "email": email, "email_verified": email_verified, "password_hash": password_hash,
               "role": role, "is_active": True, "session_version": 0, "created_at": self._now()}
        self.users[row["id"]] = row
        return self._public_user(row)

    def get_by_username(self, username):
        row = next((u for u in self.users.values() if u["username"] == username), None)
        return dict(row) if row else None

    def get_by_email(self, email):
        row = next((u for u in self.users.values() if (u.get("email") or "").lower() == email.lower()), None)
        return self._public_user(row) if row else None

    def get_user_by_id(self, user_id, with_password=False):
        row = self._user(user_id)
        if row is None:
            return None
        return dict(row) if with_password else self._public_user(row)

    def list_users(self, limit=None, offset=0, query=None):
        rows = [self._public_user(u) for u in sorted(self.users.values(), key=lambda u: u["created_at"])]
        if query:
            rows = [u for u in rows if query.lower() in u["username"].lower()]
        if limit is not None:
            rows = rows[offset:offset + limit]
        return rows

    def count_users(self, query=None):
        if query:
            return len([u for u in self.users.values() if query.lower() in u["username"].lower()])
        return len(self.users)

    def user_stats(self):
        rows = list(self.users.values())
        return {
            "total": len(rows),
            "active": len([u for u in rows if u["is_active"]]),
            "admins": len([u for u in rows if u["role"] == "admin"]),
        }

    def _update_user(self, user_id, bump=False, **fields):
        row = self._user(user_id)
        if row is None:
            return None
        row.update(fields)
        if bump:
            row["session_version"] += 1
        return self._public_user(row)

    def set_role(self, user_id, role):
        return self._update_user(user_id, bump=True, role=role)

    def set_active(self, user_id, is_active):
        return self._update_user(user_id, bump=not is_active, is_active=is_active)

    def set_password(self, user_id, password_hash):
        return self._update_user(user_id, bump=True, password_hash=password_hash)

    def bump_session_version(self, user_id):
        return self._update_user(user_id, bump=True)

    def set_email_verified(self, user_id, verified=True):
        return self._update_user(user_id, email_verified=verified)

    # ----- PostgreSQL: one-time tokens -----
    def create_token(self, user_id, token_hash, expires_at, purpose):
        self.tokens[token_hash] = {"user_id": user_id, "expires_at": expires_at, "used_at": None, "purpose": purpose}

    def _claim(self, token_hash, purpose):
        token = self.tokens.get(token_hash)
        if not token or token["purpose"] != purpose or token["used_at"] or token["expires_at"] <= datetime.now(timezone.utc):
            return None
        user = self._user(token["user_id"])
        return user if user and user["is_active"] else None

    def _burn(self, user_id, purpose):
        for token in self.tokens.values():
            if token["user_id"] == user_id and token["purpose"] == purpose and not token["used_at"]:
                token["used_at"] = datetime.now(timezone.utc)

    def token_user(self, token_hash, purpose):
        user = self._claim(token_hash, purpose)
        return self._public_user(user) if user else None

    def reset_password(self, token_hash, password_hash):
        user = self._claim(token_hash, "reset")
        if user is None:
            return None
        user.update(password_hash=password_hash, email_verified=True)
        user["session_version"] += 1
        self._burn(user["id"], "reset")
        return self._public_user(user)

    def verify_email(self, token_hash):
        user = self._claim(token_hash, "verify")
        if user is None:
            return None
        user["email_verified"] = True
        self._burn(user["id"], "verify")
        return self._public_user(user)

    def claim_key_access(self, token_hash):
        user = self._claim(token_hash, "key_access")
        if user is None:
            return None
        self._burn(user["id"], "key_access")
        return user["id"]

    # ----- PostgreSQL: app_settings -----
    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value, updated_by):
        self.settings[key] = value

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
                    "project_exists", "get_document", "get_owned_document", "create_pending", "mark_ready", "mark_failed",
                    "list_documents", "count_documents", "list_all_documents", "extension_totals", "begin_delete", "finish_delete",
                    "insert_details", "get_details", "delete_details", "update_extracted_text", "update_entity_graph", "update_ai_job",
                    "update_details", "count_with_text",
                )
            },
            chats_repo: {
                "add_messages": self.add_messages, "list_messages": self.list_messages, "list_chats": self.list_chats,
                "delete_chat": self.delete_chat, "delete_project": self.delete_project_chats,
            },
            usage_repo: {"record": self.record_llm_usage, "list_since": self.list_llm_usage_since},
            users_repo: {
                "create_user": self.create_user, "get_by_username": self.get_by_username,
                "get_by_id": self.get_user_by_id, "list_users": self.list_users,
                "count_users": self.count_users, "set_role": self.set_role, "set_active": self.set_active,
                "get_by_email": self.get_by_email, "create_token": self.create_token, "token_user": self.token_user,
                "reset_password": self.reset_password, "verify_email": self.verify_email,
                "set_password": self.set_password, "bump_session_version": self.bump_session_version,
                "set_email_verified": self.set_email_verified, "get_setting": self.get_setting, "set_setting": self.set_setting,
                "user_stats": self.user_stats, "claim_key_access": self.claim_key_access,
            },
            documents_service: {"minio_client": lambda: self, "bucket_name": lambda: "test"},
            health: {
                **{name: (lambda: None) for name in ("check_postgres", "check_mongodb", "check_minio")},
                "check_redis": lambda: "disabled",
            },
            ratelimit: {"redis_client": lambda: None},  # in-memory counts in tests
            cache: {"redis_client": lambda: None},  # no session cache in tests
            jobs: {"redis_client": lambda: None},  # no queue: media analysis runs inline
        }
        replacements[mailer] = {"send": lambda to, subject, body: self.mail.append((to, subject, body))}
        replacements[auth_service] = {"dispatch": lambda job: job()}  # "background" mail runs inline
        ratelimit.reset()
        test.addCleanup(ratelimit.reset)
        for module, functions in replacements.items():
            for name, fake in functions.items():
                patcher = patch.object(module, name, fake)
                patcher.start()
                test.addCleanup(patcher.stop)
        return self

    def sign_in(self, client, user):
        """Gives `client` a session cookie for `user` at its current session_version."""
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(user["id"], user["username"], user["role"], user["session_version"]))

    def client(self, test, role="admin", username="tester"):
        """TestClient already logged in (session cookie) as a seeded user.
        role=None gives an anonymous client."""
        client = TestClient(app)
        test.addCleanup(client.close)
        if role is not None:
            user = self.seed_user(username, role=role)
            self.sign_in(client, user)
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
