"""/admin/keys: LLM API key CRUD, gated behind a one-time link emailed to the
admin's own address (app/services/auth.py's request_key_access/confirm_key_access,
app/auth.py's key-access cookie) — a second channel beyond the session cookie.
"""
import re
import unittest

from support import FakeBackend

from app import settings


class AdminKeysTestCase(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.admin_user = self.backend.seed_user("root", role="admin", email="root@example.com", email_verified=True)
        from fastapi.testclient import TestClient
        from app.main import app
        self.admin = TestClient(app)
        self.addCleanup(self.admin.close)
        self.backend.sign_in(self.admin, self.admin_user)
        self.addCleanup(lambda: settings.update("LLM_API_KEYS", "", "cleanup"))

    def unlock(self):
        """Walks the real request -> email -> confirm flow and returns once
        this client's cookie jar holds a valid unlock."""
        self.admin.post("/admin/keys/request", follow_redirects=False)
        self.assertEqual(len(self.backend.mail), 1)
        to, subject, body = self.backend.mail[-1]
        self.assertEqual(to, "root@example.com")
        token = re.search(r"token=(\S+)", body).group(1)
        response = self.admin.get(f"/admin/keys/confirm?token={token}", follow_redirects=False)
        self.assertEqual(response.status_code, 303)


class NonAdminTests(AdminKeysTestCase):
    def test_non_admin_is_forbidden(self):
        user = self.backend.client(self, role="user", username="bob")
        self.assertEqual(user.get("/admin/keys").status_code, 403)
        self.assertEqual(user.post("/admin/keys/add", data={"value": "x"}).status_code, 403)


class LockedStateTests(AdminKeysTestCase):
    def test_page_renders_locked_by_default(self):
        html = self.admin.get("/admin/keys").text
        self.assertIn("Cần xác thực lại qua email", html)

    def test_locked_page_never_shows_a_masked_key(self):
        settings.update("LLM_API_KEYS", "a-real-key-value", "seed")
        html = self.admin.get("/admin/keys").text
        self.assertNotIn("a-real-key-value", html)
        self.assertNotIn("Khóa #1", html)

    def test_requires_a_verified_email_on_the_account(self):
        unverified = self.backend.seed_user("noemail", role="admin", email=None, email_verified=False)
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        self.addCleanup(client.close)
        self.backend.sign_in(client, unverified)
        self.assertEqual(client.post("/admin/keys/request", follow_redirects=False).status_code, 409)

    def test_mutating_actions_are_blocked_without_the_unlock_cookie(self):
        self.assertEqual(self.admin.post("/admin/keys/add", data={"value": "sneaky"}).status_code, 403)
        settings.update("LLM_API_KEYS", "existing", "seed")
        self.assertEqual(self.admin.post("/admin/keys/0/delete").status_code, 403)
        self.assertEqual(self.admin.post("/admin/keys/0/edit", data={"value": "x"}).status_code, 403)
        self.assertEqual(settings.effective("LLM_API_KEYS"), "existing")


class UnlockFlowTests(AdminKeysTestCase):
    def test_confirming_the_emailed_link_unlocks_the_page(self):
        self.unlock()
        html = self.admin.get("/admin/keys").text
        self.assertNotIn("Cần xác thực lại qua email", html)

    def test_an_unknown_or_reused_token_is_rejected(self):
        self.assertEqual(self.admin.get("/admin/keys/confirm?token=not-a-real-token").status_code, 400)
        self.unlock()
        # same client, follow the very first link again — already burned
        self.admin.post("/admin/keys/request")
        to, subject, body = self.backend.mail[-1]
        token = re.search(r"token=(\S+)", body).group(1)
        self.admin.get(f"/admin/keys/confirm?token={token}")
        self.assertEqual(self.admin.get(f"/admin/keys/confirm?token={token}").status_code, 400)

    def test_unlock_does_not_carry_over_to_a_different_account(self):
        self.unlock()
        unlock_cookie = self.admin.cookies.get("rdw_key_access")
        other = self.backend.seed_user("other-admin", role="admin", email="other@example.com", email_verified=True)
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        self.addCleanup(client.close)
        self.backend.sign_in(client, other)
        client.cookies.set("rdw_key_access", unlock_cookie)
        self.assertEqual(client.post("/admin/keys/add", data={"value": "x"}).status_code, 403)


class KeyCrudTests(AdminKeysTestCase):
    def test_add_list_edit_delete_round_trip(self):
        self.unlock()
        self.admin.post("/admin/keys/add", data={"value": "first-key"})
        self.admin.post("/admin/keys/add", data={"value": "second-key"})
        self.assertEqual(settings.effective("LLM_API_KEYS"), "first-key,second-key")

        html = self.admin.get("/admin/keys").text
        self.assertNotIn("first-key", html)
        self.assertNotIn("second-key", html)
        self.assertIn("Khóa #1", html)
        self.assertIn("Khóa #2", html)

        self.admin.post("/admin/keys/0/edit", data={"value": "replaced-key"})
        self.assertEqual(settings.effective("LLM_API_KEYS"), "replaced-key,second-key")

        self.admin.post("/admin/keys/0/delete")
        self.assertEqual(settings.effective("LLM_API_KEYS"), "second-key")

    def test_add_rejects_blank_and_duplicate(self):
        self.unlock()
        self.assertEqual(self.admin.post("/admin/keys/add", data={"value": "  "}).status_code, 422)
        self.admin.post("/admin/keys/add", data={"value": "dup-key"})
        self.assertEqual(self.admin.post("/admin/keys/add", data={"value": "dup-key"}).status_code, 409)

    def test_delete_unknown_index_is_404(self):
        self.unlock()
        self.assertEqual(self.admin.post("/admin/keys/5/delete").status_code, 404)


if __name__ == "__main__":
    unittest.main()
