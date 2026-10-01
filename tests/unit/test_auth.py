"""Login and access control: password hashing, signed session cookies, the
login/logout flow and which pages each role can reach.
"""
import unittest
from unittest.mock import patch
from uuid import uuid4

from support import FakeBackend

from app import auth


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
        token = auth.create_session_token(user_id, "alice", "admin", 3)
        self.assertEqual(auth.verify_session_token(token), {"user_id": str(user_id), "username": "alice", "role": "admin", "session_version": 3})

    def test_cookies_from_before_session_versions_are_rejected(self):
        import hashlib, hmac, os, time
        payload = f"{uuid4()}:alice:admin:{int(time.time()) + 3600}"
        signature = hmac.new(os.environ["SESSION_SECRET"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        self.assertIsNone(auth.verify_session_token(f"{payload}:{signature}"))

    def test_missing_or_malformed_token_rejected(self):
        for token in (None, "", "not-enough-parts"):
            with self.subTest(token=token):
                self.assertIsNone(auth.verify_session_token(token))

    def test_tampered_signature_rejected(self):
        token = auth.create_session_token(uuid4(), "alice", "user")
        payload, _, signature = token.rpartition(":")
        flipped = ("1" if signature[0] == "0" else "0") + signature[1:]
        self.assertIsNone(auth.verify_session_token(payload + ":" + flipped))

    def test_role_escalation_without_valid_signature_rejected(self):
        parts = auth.create_session_token(uuid4(), "alice", "user").split(":")
        parts[2] = "admin"
        self.assertIsNone(auth.verify_session_token(":".join(parts)))

    def test_expired_token_rejected(self):
        with patch("app.auth.time") as mocked_time:
            mocked_time.time.return_value = 1_000_000  # 1970-01-12 — long expired by now
            token = auth.create_session_token(uuid4(), "alice", "user")
        self.assertIsNone(auth.verify_session_token(token))


class LoginFlowTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.client = self.backend.client(self, role=None)

    def login(self, username, password):
        return self.client.post("/login", data={"username": username, "password": password}, follow_redirects=False)

    def test_successful_login_sets_cookie_and_opens_the_app(self):
        self.backend.seed_user("alice", "correct-password")
        response = self.login("alice", "correct-password")
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/"))
        self.assertIn(auth.SESSION_COOKIE, self.client.cookies)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_rejected_logins_set_no_cookie(self):
        self.backend.seed_user("alice", "correct-password")
        self.backend.seed_user("bob", "correct-password", is_active=False)
        for username, password in (("alice", "wrong-password"), ("ghost", "whatever"), ("bob", "correct-password")):
            with self.subTest(username=username):
                self.assertEqual(self.login(username, password).status_code, 401)
                self.assertNotIn(auth.SESSION_COOKIE, self.client.cookies)

    def test_logout_clears_cookie_and_blocks_access_again(self):
        self.backend.seed_user("alice", "correct-password")
        self.login("alice", "correct-password")
        self.client.post("/logout")
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/login"))


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)

    def test_anonymous_pages_redirect_and_api_returns_401(self):
        client = self.backend.client(self, role=None)
        response = client.get("/", follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/login"))
        self.assertEqual(client.get("/projects").status_code, 401)

    def test_public_paths_need_no_login(self):
        client = self.backend.client(self, role=None)
        for path in ("/login", "/health/live", "/static/style.css"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)

    def test_only_admins_reach_account_management(self):
        self.assertEqual(self.backend.client(self, role="user", username="alice").get("/admin/users").status_code, 403)
        self.assertEqual(self.backend.client(self, role="admin", username="root").get("/admin/users").status_code, 200)


if __name__ == "__main__":
    unittest.main()
