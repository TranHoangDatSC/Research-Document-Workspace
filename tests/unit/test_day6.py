"""Run with: python -m unittest discover -s tests/unit -v
Admin-managed accounts (Day 6): password hashing, session cookies, and the
login/admin-gating HTTP flow. Storage doubles, no Docker needed.
"""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-production")

import psycopg
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.repositories import users as repository
from app.services import auth as service


class PasswordHashingTests(unittest.TestCase):
    def test_round_trip(self):
        stored = auth.hash_password("correct horse battery staple")
        self.assertTrue(auth.verify_password("correct horse battery staple", stored))

    def test_wrong_password_rejected(self):
        stored = auth.hash_password("correct horse battery staple")
        self.assertFalse(auth.verify_password("wrong password", stored))

    def test_malformed_stored_hash_rejected_not_raised(self):
        self.assertFalse(auth.verify_password("anything", "not-a-valid-hash"))

    def test_two_hashes_of_same_password_differ(self):
        # Random salt per call is what makes a precomputed rainbow table useless.
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))


class SessionTokenTests(unittest.TestCase):
    def test_round_trip(self):
        user_id = uuid4()
        token = auth.create_session_token(user_id, "alice", "admin")
        session = auth.verify_session_token(token)
        self.assertEqual(session, {"user_id": str(user_id), "username": "alice", "role": "admin"})

    def test_missing_token_rejected(self):
        self.assertIsNone(auth.verify_session_token(None))
        self.assertIsNone(auth.verify_session_token(""))

    def test_malformed_token_rejected(self):
        self.assertIsNone(auth.verify_session_token("not-enough-parts"))

    def test_tampered_signature_rejected(self):
        token = auth.create_session_token(uuid4(), "alice", "user")
        payload, _, signature = token.rpartition(":")
        flipped = ("1" if signature[0] == "0" else "0") + signature[1:]
        self.assertIsNone(auth.verify_session_token(payload + ":" + flipped))

    def test_role_escalation_without_valid_signature_rejected(self):
        token = auth.create_session_token(uuid4(), "alice", "user")
        parts = token.split(":")
        parts[2] = "admin"
        self.assertIsNone(auth.verify_session_token(":".join(parts)))

    def test_expired_token_rejected(self):
        with patch("app.auth.time") as mocked_time:
            mocked_time.time.return_value = 1_000_000  # 1970-01-12 — long expired by now
            token = auth.create_session_token(uuid4(), "alice", "user")
        self.assertIsNone(auth.verify_session_token(token))


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.users = {}

    def seed(self, username, password, role="user", is_active=True):
        self.users[username] = {
            "id": uuid4(), "username": username, "role": role,
            "is_active": is_active, "created_at": datetime.now(timezone.utc),
            "password_hash": auth.hash_password(password),
        }

    def test_authenticate_success_excludes_password_hash(self):
        self.seed("alice", "correct-password")
        with patch.object(repository, "get_by_username", lambda u: self.users.get(u)):
            user = service.authenticate("alice", "correct-password")
        self.assertEqual(user["username"], "alice")
        self.assertNotIn("password_hash", user)

    def test_authenticate_wrong_password(self):
        self.seed("alice", "correct-password")
        with patch.object(repository, "get_by_username", lambda u: self.users.get(u)):
            self.assertIsNone(service.authenticate("alice", "wrong-password"))

    def test_authenticate_unknown_user(self):
        with patch.object(repository, "get_by_username", lambda u: None):
            self.assertIsNone(service.authenticate("ghost", "whatever"))

    def test_authenticate_inactive_user_rejected(self):
        self.seed("alice", "correct-password", is_active=False)
        with patch.object(repository, "get_by_username", lambda u: self.users.get(u)):
            self.assertIsNone(service.authenticate("alice", "correct-password"))

    def test_create_user_rejects_bad_username(self):
        with self.assertRaises(HTTPException) as caught:
            service.create_user("a", "longenoughpassword", "user")
        self.assertEqual(caught.exception.status_code, 422)

    def test_create_user_rejects_short_password(self):
        with self.assertRaises(HTTPException) as caught:
            service.create_user("valid_name", "short", "user")
        self.assertEqual(caught.exception.status_code, 422)

    def test_create_user_rejects_bad_role(self):
        with self.assertRaises(HTTPException) as caught:
            service.create_user("valid_name", "longenoughpassword", "superadmin")
        self.assertEqual(caught.exception.status_code, 422)

    def test_create_user_duplicate_username_returns_409(self):
        def raise_conflict(username, password_hash, role):
            raise psycopg.errors.UniqueViolation("duplicate")
        with patch.object(repository, "create_user", raise_conflict):
            with self.assertRaises(HTTPException) as caught:
                service.create_user("valid_name", "longenoughpassword", "user")
        self.assertEqual(caught.exception.status_code, 409)

    def test_set_role_blocks_self_change(self):
        user_id = uuid4()
        with self.assertRaises(HTTPException) as caught:
            service.set_role(user_id, "admin", user_id)
        self.assertEqual(caught.exception.status_code, 400)

    def test_set_active_blocks_self_change(self):
        user_id = uuid4()
        with self.assertRaises(HTTPException) as caught:
            service.set_active(user_id, False, user_id)
        self.assertEqual(caught.exception.status_code, 400)

    def test_set_role_missing_user_returns_404(self):
        with patch.object(repository, "set_role", lambda uid, role: None):
            with self.assertRaises(HTTPException) as caught:
                service.set_role(uuid4(), "admin", uuid4())
        self.assertEqual(caught.exception.status_code, 404)


class LoginFlowTests(unittest.TestCase):
    """Full HTTP flow with a fake users repository — real middleware, real routes."""

    def setUp(self):
        self.users = {}

        def get_by_username(username):
            return self.users.get(username)

        def list_users():
            return [
                {k: v for k, v in u.items() if k != "password_hash"}
                for u in self.users.values()
            ]

        patches = [
            patch.object(repository, "get_by_username", get_by_username),
            patch.object(repository, "list_users", list_users),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def seed(self, username, password, role="user", is_active=True):
        self.users[username] = {
            "id": uuid4(), "username": username, "role": role,
            "is_active": is_active, "created_at": datetime.now(timezone.utc),
            "password_hash": auth.hash_password(password),
        }

    def login(self, username, password):
        return self.client.post("/login", data={"username": username, "password": password}, follow_redirects=False)

    def test_unauthenticated_ui_redirects_to_login(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")

    def test_unauthenticated_api_returns_401_json(self):
        r = self.client.get("/projects")
        self.assertEqual(r.status_code, 401)

    def test_health_and_static_are_public(self):
        self.assertEqual(self.client.get("/health/live").status_code, 200)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)

    def test_login_page_itself_is_public(self):
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_wrong_password_rejected_without_cookie(self):
        self.seed("alice", "correct-password")
        r = self.login("alice", "wrong-password")
        self.assertEqual(r.status_code, 401)
        self.assertNotIn(auth.SESSION_COOKIE, self.client.cookies)

    def test_unknown_username_rejected(self):
        r = self.login("ghost", "whatever")
        self.assertEqual(r.status_code, 401)

    def test_inactive_user_cannot_log_in(self):
        self.seed("bob", "correct-password", is_active=False)
        r = self.login("bob", "correct-password")
        self.assertEqual(r.status_code, 401)

    def test_successful_login_grants_access_past_the_auth_gate(self):
        self.seed("root", "correct-password", role="admin")
        r = self.login("root", "correct-password")
        self.assertEqual(r.status_code, 303)
        self.assertIn(auth.SESSION_COOKIE, self.client.cookies)
        self.assertEqual(self.client.get("/admin/users").status_code, 200)

    def test_logout_clears_cookie_and_reblocks_access(self):
        self.seed("alice", "correct-password")
        self.login("alice", "correct-password")
        self.client.post("/logout")
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")

    def test_regular_user_cannot_reach_admin_page(self):
        self.seed("alice", "correct-password", role="user")
        self.login("alice", "correct-password")
        self.assertEqual(self.client.get("/admin/users").status_code, 403)

    def test_admin_can_reach_admin_page(self):
        self.seed("root", "correct-password", role="admin")
        self.login("root", "correct-password")
        self.assertEqual(self.client.get("/admin/users").status_code, 200)


if __name__ == "__main__":
    unittest.main()
