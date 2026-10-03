"""Account management (admin only): create, view, update role / lock.
Accounts are locked rather than deleted, so there is no delete step.
"""
import unittest
from uuid import uuid4

from support import FakeBackend


class UserTestCase(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.admin = self.backend.client(self, role="admin", username="root")

    def find(self, username):
        return next(u for u in self.backend.users.values() if u["username"] == username)

    def create(self, username="alice", password="long-enough-password", role="user"):
        return self.admin.post("/admin/users", data={"username": username, "password": password, "role": role}, follow_redirects=False)


class CreateUserTests(UserTestCase):
    def test_admin_creates_account_that_can_log_in(self):
        response = self.create("alice", "long-enough-password", "user")
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/admin/users"))
        self.assertEqual(self.find("alice")["role"], "user")
        # The password is stored hashed, and works for a real login.
        self.assertNotEqual(self.find("alice")["password_hash"], "long-enough-password")
        visitor = self.backend.client(self, role=None)
        login = visitor.post("/login", data={"username": "alice", "password": "long-enough-password"}, follow_redirects=False)
        self.assertEqual(login.status_code, 303)

    def test_rejects_invalid_accounts(self):
        cases = [
            ({"username": "a"}, 422),                    # username too short
            ({"username": "bad name!"}, 422),            # characters not allowed
            ({"password": "short"}, 422),                # password under 8 characters
            ({"role": "superadmin"}, 422),               # unknown role
        ]
        for override, status in cases:
            with self.subTest(override=override):
                self.assertEqual(self.create(**override).status_code, status)
        self.assertEqual([u["username"] for u in self.backend.users.values()], ["root"])

    def test_duplicate_username_is_409(self):
        self.create("alice")
        self.assertEqual(self.create("alice").status_code, 409)

    def test_regular_user_cannot_create_accounts(self):
        user = self.backend.client(self, role="user", username="bob")
        response = user.post("/admin/users", data={"username": "mallory", "password": "long-enough-password", "role": "admin"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("mallory", [u["username"] for u in self.backend.users.values()])


class ViewUserTests(UserTestCase):
    def test_list_shows_accounts_without_password_hashes(self):
        self.create("alice")
        html = self.admin.get("/admin/users").text
        self.assertIn("root", html)
        self.assertIn("alice", html)
        self.assertNotIn(self.find("alice")["password_hash"], html)

    def test_list_paginates_past_the_page_size(self):
        for i in range(25):
            self.create(f"user{i:02d}")
        first = self.admin.get("/admin/users").text
        self.assertIn('class="pager-num current"', first)
        self.assertIn("user00", first)
        self.assertNotIn("user24", first)  # on page 2, not page 1
        second = self.admin.get("/admin/users?page=2").text
        self.assertIn("user24", second)

    def test_search_finds_a_user_regardless_of_page(self):
        for i in range(25):
            self.create(f"user{i:02d}")
        html = self.admin.get("/admin/users?q=user24").text
        self.assertIn("user24", html)
        self.assertNotIn("user00", html)

    def test_stat_row_counts_every_account_not_just_the_page(self):
        for i in range(25):
            self.create(f"user{i:02d}")
        html = self.admin.get("/admin/users").text
        self.assertIn("<strong>26</strong>", html)  # root + 25 created, across both pages


class UpdateUserTests(UserTestCase):
    def test_change_role(self):
        self.create("alice")
        alice = self.find("alice")
        response = self.admin.post(f"/admin/users/{alice['id']}/role", data={"role": "admin"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(alice["role"], "admin")

    def test_lock_blocks_login_and_unlock_restores_it(self):
        self.create("alice", "long-enough-password")
        alice = self.find("alice")
        visitor = self.backend.client(self, role=None)
        login = {"username": "alice", "password": "long-enough-password"}

        self.admin.post(f"/admin/users/{alice['id']}/active", data={"is_active": "false"})
        self.assertFalse(alice["is_active"])
        self.assertEqual(visitor.post("/login", data=login, follow_redirects=False).status_code, 401)

        self.admin.post(f"/admin/users/{alice['id']}/active", data={"is_active": "true"})
        self.assertEqual(visitor.post("/login", data=login, follow_redirects=False).status_code, 303)

    def test_admin_cannot_demote_or_lock_themself(self):
        me = self.admin.user["id"]
        self.assertEqual(self.admin.post(f"/admin/users/{me}/role", data={"role": "user"}).status_code, 400)
        self.assertEqual(self.admin.post(f"/admin/users/{me}/active", data={"is_active": "false"}).status_code, 400)
        self.assertEqual(self.find("root")["role"], "admin")
        self.assertTrue(self.find("root")["is_active"])

    def test_invalid_role_and_unknown_user(self):
        self.create("alice")
        alice = self.find("alice")
        self.assertEqual(self.admin.post(f"/admin/users/{alice['id']}/role", data={"role": "owner"}).status_code, 422)
        self.assertEqual(self.admin.post(f"/admin/users/{uuid4()}/role", data={"role": "admin"}).status_code, 404)
        self.assertEqual(self.admin.post(f"/admin/users/{uuid4()}/active", data={"is_active": "false"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
