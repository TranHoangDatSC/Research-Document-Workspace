"""Accounts: per-user data isolation, sign-up (closed by default, admin
toggle, email verification), session revocation, password reset/change,
security hardening, and the SMTP mailer.
"""
import hashlib
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

from support import FakeBackend, upload

from app import auth, mailer


class AccountTestCase(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.backend.settings["signup_open"] = "true"  # most tests need sign-up open
        self.visitor = self.backend.client(self, role=None)
        env = patch.dict(os.environ, {"APP_BASE_URL": "https://rdw.example.edu", "ALLOW_SIGNUP": "true"})
        env.start()
        self.addCleanup(env.stop)

    def signup(self, client=None, **overrides):
        form = {"username": "alice", "email": "alice@example.edu", "password": "long-enough-1", "password_confirm": "long-enough-1", **overrides}
        return (client or self.visitor).post("/signup", data=form, follow_redirects=False)

    def login(self, username, password, client=None):
        return (client or self.backend.client(self, role=None)).post("/login", data={"username": username, "password": password}, follow_redirects=False)

    def user(self, username):
        return next(u for u in self.backend.users.values() if u["username"] == username)


class DataIsolationTests(AccountTestCase):
    """Anyone can sign up now, so nobody may see another account's work."""

    def setUp(self):
        super().setUp()
        self.alice = self.backend.client(self, role="user", username="alice")
        self.bob = self.backend.client(self, role="user", username="bob")
        self.project = self.alice.post("/projects", json={"name": "Alice research"}).json()
        self.document = upload(self.alice, self.project["id"])

    def test_other_users_projects_are_invisible(self):
        pid = self.project["id"]
        self.assertEqual(self.bob.get("/projects").json(), [])
        for method, path, kwargs in (
            ("get", f"/projects/{pid}", {}),
            ("put", f"/projects/{pid}", {"json": {"name": "hijacked"}}),
            ("delete", f"/projects/{pid}", {}),
            ("get", f"/projects/{pid}/documents", {}),
            ("post", f"/projects/{pid}/documents", {"files": {"file": ("x.txt", b"x")}}),
            ("get", f"/ui/projects/{pid}", {}),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(getattr(self.bob, method)(path, **kwargs).status_code, 404)
        self.assertEqual(self.backend.projects[UUID(pid)]["name"], "Alice research")
        self.assertNotIn("Alice research", self.bob.get("/").text)

    def test_other_users_documents_are_invisible(self):
        did = self.document["id"]
        for method, path, kwargs in (
            ("get", f"/documents/{did}", {}),
            ("get", f"/documents/{did}/download", {}),
            ("get", f"/documents/{did}/content", {}),
            ("patch", f"/documents/{did}", {"data": {"tags": "hijacked"}}),
            ("post", f"/documents/{did}/extract", {}),
            ("delete", f"/documents/{did}", {}),
            ("get", f"/ui/documents/{did}", {}),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(getattr(self.bob, method)(path, **kwargs).status_code, 404)
        self.assertIn(UUID(did), self.backend.documents)
        self.assertEqual(self.backend.details[did]["tags"], ["cloud", "docker"])

    def test_owner_keeps_full_access(self):
        self.assertEqual([p["name"] for p in self.alice.get("/projects").json()], ["Alice research"])
        self.assertEqual(self.alice.get(f"/documents/{self.document['id']}").status_code, 200)
        self.assertEqual(self.alice.delete(f"/documents/{self.document['id']}").status_code, 200)

    def test_admins_see_only_their_own_projects_too(self):
        admin = self.backend.client(self, role="admin", username="root")
        self.assertEqual(admin.get(f"/projects/{self.project['id']}").status_code, 404)


class SignupSettingTests(AccountTestCase):
    """Closed by default (internal tool); an admin opens/closes it; .env can lock it shut."""

    def test_closed_by_default(self):
        self.backend.settings.clear()
        self.assertEqual(self.visitor.get("/signup").status_code, 403)
        self.assertEqual(self.signup().status_code, 403)
        self.assertNotIn('href="/signup"', self.visitor.get("/login").text)
        self.assertEqual(self.backend.users, {})

    def test_admin_opens_and_closes_it(self):
        self.backend.settings.clear()
        admin = self.backend.client(self, role="admin", username="root")
        self.assertIn("đang đóng", admin.get("/admin/users").text)
        admin.post("/admin/settings/signup", data={"open": "true"})
        self.assertEqual(self.visitor.get("/signup").status_code, 200)
        self.assertIn("đang mở", admin.get("/admin/users").text)
        admin.post("/admin/settings/signup", data={"open": "false"})
        self.assertEqual(self.visitor.get("/signup").status_code, 403)

    def test_env_kill_switch_beats_the_admin_setting(self):
        admin = self.backend.client(self, role="admin", username="root")
        with patch.dict(os.environ, {"ALLOW_SIGNUP": "false"}):
            self.assertEqual(self.visitor.get("/signup").status_code, 403)
            self.assertEqual(admin.post("/admin/settings/signup", data={"open": "true"}).status_code, 409)
            self.assertIn("ALLOW_SIGNUP=false", admin.get("/admin/users").text)

    def test_regular_users_cannot_change_it(self):
        self.backend.settings.clear()
        user = self.backend.client(self, role="user", username="bob")
        self.assertEqual(user.post("/admin/settings/signup", data={"open": "true"}).status_code, 403)
        self.assertEqual(self.backend.settings, {})


class SignupAndVerificationTests(AccountTestCase):
    def emailed_link(self, path):
        to, _, body = self.backend.mail[-1]
        return to, re.search(rf"https?://\S+{path}\?token=\S+", body).group(0)

    def test_signup_then_verify_then_login(self):
        response = self.signup(role="admin")  # an injected role field is ignored
        self.assertEqual(response.status_code, 201)
        self.assertNotIn(auth.SESSION_COOKIE, response.cookies)  # no session before verification
        self.assertIn("Mở liên kết vừa gửi tới", response.text)
        alice = self.user("alice")
        self.assertEqual((alice["role"], alice["email_verified"]), ("user", False))

        blocked = self.login("alice", "long-enough-1")
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("chưa xác minh email", blocked.text)
        self.assertIn("/verify-email/resend?email=alice%40example.edu", blocked.text)

        to, link = self.emailed_link("/verify-email")
        self.assertEqual(to, "alice@example.edu")
        self.assertTrue(link.startswith("https://rdw.example.edu/verify-email?token="))
        verified = self.visitor.get(link.replace("https://rdw.example.edu", ""), follow_redirects=False)
        self.assertEqual(verified.headers["location"], "/login?done=verified")
        self.assertTrue(alice["email_verified"])
        self.assertEqual(self.login("alice", "long-enough-1").status_code, 303)
        # The link worked once; a second use is refused.
        self.assertEqual(self.visitor.get(link.replace("https://rdw.example.edu", "")).status_code, 400)

    def test_resend_verification_is_neutral(self):
        self.signup()
        self.backend.mail.clear()
        sent = self.visitor.post("/verify-email/resend", data={"email": "alice@example.edu"})
        self.assertIn("email xác minh mới vừa được gửi", sent.text)
        self.assertEqual(len(self.backend.mail), 1)
        nobody = self.visitor.post("/verify-email/resend", data={"email": "nobody@example.edu"})
        self.assertEqual(nobody.text.replace("nobody@example.edu", "X"), sent.text.replace("alice@example.edu", "X"))
        self.assertEqual(len(self.backend.mail), 1)

    def test_admin_can_verify_by_hand(self):
        self.signup()
        admin = self.backend.client(self, role="admin", username="root")
        self.assertIn("chưa xác minh email", admin.get("/admin/users").text)
        admin.post(f"/admin/users/{self.user('alice')['id']}/verify")
        self.assertTrue(self.user("alice")["email_verified"])

    def test_admin_created_accounts_are_verified(self):
        admin = self.backend.client(self, role="admin", username="root")
        admin.post("/admin/users", data={"username": "carol", "password": "long-enough-1", "role": "user", "email": "carol@example.edu"})
        self.assertTrue(self.user("carol")["email_verified"])
        self.assertEqual(self.login("carol", "long-enough-1").status_code, 303)

    def test_invalid_signups_keep_what_was_typed(self):
        cases = [
            ({"password_confirm": "different-1"}, 422, "không khớp"),
            ({"email": ""}, 422, "Cần nhập email"),
            ({"email": "not-an-email"}, 422, "Email không hợp lệ"),
            ({"username": "x"}, 422, "Tên đăng nhập phải có"),
            ({"password": "short", "password_confirm": "short"}, 422, "ít nhất 8"),
        ]
        for override, status, message in cases:
            with self.subTest(override=override):
                response = self.signup(**override)
                self.assertEqual(response.status_code, status)
                self.assertIn(message, response.text)
        self.assertIn('value="alice@example.edu"', self.signup(password_confirm="nope-nope-1").text)
        self.assertEqual(self.backend.users, {})

    def test_duplicate_username_or_email(self):
        self.signup()
        again = self.backend.client(self, role=None)
        self.assertIn("Tên đăng nhập đã có người dùng", self.signup(again, email="other@example.edu").text)
        response = self.signup(again, username="alice2", email="ALICE@example.edu")  # case-insensitive
        self.assertEqual(response.status_code, 409)
        self.assertIn("Email này đã được dùng", response.text)

    def test_signup_is_rate_limited(self):
        for i in range(5):
            self.signup(username=f"user{i}", email=f"user{i}@example.edu")
        response = self.signup(username="user9", email="user9@example.edu")
        self.assertEqual(response.status_code, 429)
        self.assertIn("Thử quá nhiều lần", response.text)


class SessionRevocationTests(AccountTestCase):
    """Cookies are stateless, but every request checks session_version, so
    revoking takes effect on the very next request."""

    def setUp(self):
        super().setUp()
        self.alice_row = self.backend.seed_user("alice", "old-password-1", email="alice@example.edu")
        self.laptop = self.backend.client(self, role=None)
        self.phone = self.backend.client(self, role=None)
        for device in (self.laptop, self.phone):
            self.assertEqual(self.login("alice", "old-password-1", device).status_code, 303)

    def signed_in(self, client):
        return client.get("/", follow_redirects=False).status_code == 200

    def test_password_change_signs_out_other_devices_only(self):
        response = self.laptop.post("/account/password", data={"current_password": "old-password-1", "password": "new-password-1", "password_confirm": "new-password-1"}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/account?changed=1")
        self.assertTrue(self.signed_in(self.laptop))
        self.assertFalse(self.signed_in(self.phone))
        self.assertIn("Các thiết bị khác đã bị đăng xuất", self.laptop.get("/account?changed=1").text)

    def test_password_change_needs_the_current_password(self):
        response = self.laptop.post("/account/password", data={"current_password": "wrong", "password": "new-password-1", "password_confirm": "new-password-1"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Mật khẩu hiện tại không đúng", response.text)
        self.assertTrue(self.signed_in(self.phone))

    def test_logout_everywhere(self):
        response = self.laptop.post("/account/logout-everywhere", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/login?done=signed-out")
        self.assertFalse(self.signed_in(self.laptop))
        self.assertFalse(self.signed_in(self.phone))

    def test_locking_ends_sessions_at_once(self):
        admin = self.backend.client(self, role="admin", username="root")
        admin.post(f"/admin/users/{self.alice_row['id']}/active", data={"is_active": "false"})
        self.assertFalse(self.signed_in(self.laptop))
        self.assertEqual(self.phone.get("/projects").status_code, 401)
        admin.post(f"/admin/users/{self.alice_row['id']}/active", data={"is_active": "true"})
        self.assertFalse(self.signed_in(self.laptop))  # unlocking doesn't revive old cookies

    def test_role_change_applies_immediately(self):
        root = self.backend.seed_user("root2", "admin-password-1", role="admin")
        boss = self.backend.client(self, role=None)
        self.login("root2", "admin-password-1", boss)
        self.assertEqual(boss.get("/admin/users").status_code, 200)
        other_admin = self.backend.client(self, role="admin", username="root")
        other_admin.post(f"/admin/users/{root['id']}/role", data={"role": "user"})
        self.assertFalse(self.signed_in(boss))  # demoted: old admin cookie is dead

    def test_password_reset_ends_sessions(self):
        self.visitor.post("/forgot-password", data={"email": "alice@example.edu"})
        token = re.search(r"token=(\S+)", self.backend.mail[-1][2]).group(1)
        self.visitor.post("/reset-password", data={"token": token, "password": "new-password-1", "password_confirm": "new-password-1"})
        self.assertFalse(self.signed_in(self.laptop))
        self.assertFalse(self.signed_in(self.phone))

    def test_revoked_cookie_is_cleared(self):
        self.laptop.post("/account/logout-everywhere")
        response = self.phone.get("/", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/login")
        self.assertIn(auth.SESSION_COOKIE, response.headers.get("set-cookie", ""))  # deleted


class PasswordResetTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.backend.seed_user("alice", "old-password-1", email="alice@example.edu")

    def request_reset(self, email="alice@example.edu", client=None):
        return (client or self.visitor).post("/forgot-password", data={"email": email})

    def emailed_link(self):
        to, subject, body = self.backend.mail[-1]
        return to, re.search(r"https?://\S+/reset-password\?token=\S+", body).group(0)

    def test_full_flow(self):
        response = self.request_reset("ALICE@example.edu")
        self.assertIn("liên kết đặt lại mật khẩu vừa được gửi", response.text)
        to, link = self.emailed_link()
        self.assertEqual(to, "alice@example.edu")
        self.assertTrue(link.startswith("https://rdw.example.edu/reset-password?token="))
        token = link.split("token=")[1]
        # Only a hash is stored: the database never holds a working link.
        self.assertNotIn(token, self.backend.tokens)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), self.backend.tokens)

        page = self.visitor.get("/reset-password", params={"token": token})
        self.assertIn("alice", page.text)
        done = self.visitor.post("/reset-password", data={"token": token, "password": "new-password-1", "password_confirm": "new-password-1"}, follow_redirects=False)
        self.assertEqual(done.headers["location"], "/login?done=reset")
        self.assertIn("Đã đổi mật khẩu", self.visitor.get("/login?done=reset").text)
        self.assertEqual(self.login("alice", "new-password-1").status_code, 303)
        self.assertEqual(self.login("alice", "old-password-1").status_code, 401)

        again = self.visitor.post("/reset-password", data={"token": token, "password": "another-pass-1", "password_confirm": "another-pass-1"})
        self.assertEqual(again.status_code, 400)
        self.assertIn("không hợp lệ hoặc đã hết hạn", again.text)

    def test_reset_link_also_verifies_the_email(self):
        self.user("alice")["email_verified"] = False
        self.request_reset()
        token = self.emailed_link()[1].split("token=")[1]
        self.visitor.post("/reset-password", data={"token": token, "password": "new-password-1", "password_confirm": "new-password-1"})
        self.assertTrue(self.user("alice")["email_verified"])

    def test_a_verify_token_cannot_reset_a_password(self):
        self.user("alice")["email_verified"] = False
        self.visitor.post("/verify-email/resend", data={"email": "alice@example.edu"})
        verify_token = re.search(r"token=(\S+)", self.backend.mail[-1][2]).group(1)
        response = self.visitor.post("/reset-password", data={"token": verify_token, "password": "new-password-1", "password_confirm": "new-password-1"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_or_locked_accounts_get_the_same_answer_and_no_mail(self):
        known = self.request_reset().text
        self.backend.mail.clear()
        unknown = self.request_reset("nobody@example.edu").text
        self.assertEqual(self.backend.mail, [])
        self.assertEqual(known.replace("alice@example.edu", "X"), unknown.replace("nobody@example.edu", "X"))
        self.user("alice")["is_active"] = False
        self.request_reset()
        self.assertEqual(self.backend.mail, [])

    def test_expired_link_is_refused(self):
        self.request_reset()
        token = self.emailed_link()[1].split("token=")[1]
        for row in self.backend.tokens.values():
            row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertIn("Liên kết không hợp lệ", self.visitor.get("/reset-password", params={"token": token}).text)
        response = self.visitor.post("/reset-password", data={"token": token, "password": "new-password-1", "password_confirm": "new-password-1"})
        self.assertEqual(response.status_code, 400)

    def test_using_one_link_burns_the_others(self):
        self.request_reset()
        first = self.emailed_link()[1].split("token=")[1]
        self.request_reset()
        second = self.emailed_link()[1].split("token=")[1]
        self.visitor.post("/reset-password", data={"token": second, "password": "new-password-1", "password_confirm": "new-password-1"})
        self.assertEqual(self.visitor.post("/reset-password", data={"token": first, "password": "x-password-1", "password_confirm": "x-password-1"}).status_code, 400)

    def test_mismatched_new_password_keeps_the_form(self):
        self.request_reset()
        token = self.emailed_link()[1].split("token=")[1]
        response = self.visitor.post("/reset-password", data={"token": token, "password": "new-password-1", "password_confirm": "other-password-1"})
        self.assertEqual(response.status_code, 422)
        self.assertIn(f'value="{token}"', response.text)

    def test_link_never_follows_a_forged_host_header(self):
        self.visitor.post("/forgot-password", data={"email": "alice@example.edu"}, headers={"Host": "evil.example"})
        self.assertTrue(self.backend.mail)
        self.assertTrue(all("evil.example" not in body for _, _, body in self.backend.mail))

    def test_no_mail_when_smtp_is_set_but_base_url_is_not(self):
        with patch.dict(os.environ, {"APP_BASE_URL": ""}), patch.object(mailer, "configured", lambda: True):
            response = self.request_reset()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.backend.mail, [])

    def test_forgot_password_is_rate_limited(self):
        for _ in range(5):
            self.request_reset("nobody@example.edu")
        self.assertEqual(self.request_reset("nobody@example.edu").status_code, 429)


class SecurityHardeningTests(AccountTestCase):
    def test_login_is_rate_limited(self):
        self.backend.seed_user("alice", "right-password")
        client = self.backend.client(self, role=None)
        for _ in range(20):
            self.login("alice", "wrong", client)
        response = self.login("alice", "right-password", client)
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)

    def test_unknown_username_costs_a_password_check_too(self):
        # No fast path that would reveal which usernames exist by timing.
        with patch.object(auth, "verify_password", wraps=auth.verify_password) as checked:
            self.login("ghost", "whatever")
        self.assertEqual(checked.call_count, 1)

    def test_account_forms_reject_cross_site_posts(self):
        for path in ("/login", "/signup", "/forgot-password", "/reset-password", "/verify-email/resend"):
            with self.subTest(path=path):
                response = self.visitor.post(path, data={}, headers={"Origin": "https://attacker.example"})
                self.assertEqual(response.status_code, 403)

    def test_forms_accepted_from_https_behind_the_proxy(self):
        # Caddy terminates TLS: the browser's Origin is https://host while the
        # app sees http://host — must not be mistaken for a cross-site post.
        self.backend.seed_user("alice", "right-password")
        response = self.visitor.post("/login", data={"username": "alice", "password": "right-password"},
                                     headers={"Origin": "https://testserver", "X-Forwarded-Proto": "https"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("secure", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)

    def test_security_headers_on_every_response(self):
        for path in ("/login", "/health/live"):
            with self.subTest(path=path):
                headers = self.visitor.get(path).headers
                self.assertEqual(headers["x-frame-options"], "DENY")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["referrer-policy"], "same-origin")
                self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertIn("strict-transport-security", self.visitor.get("/login", headers={"X-Forwarded-Proto": "https"}).headers)

    def test_api_docs_need_login(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(self.visitor.get(path, follow_redirects=False).status_code, 401)

    def test_login_page_links(self):
        html = self.visitor.get("/login").text
        self.assertIn('href="/forgot-password"', html)
        self.assertIn('href="/signup"', html)  # open in these tests

    def test_admin_can_give_an_email(self):
        admin = self.backend.client(self, role="admin", username="root")
        admin.post("/admin/users", data={"username": "carol", "password": "long-enough-1", "role": "user", "email": "carol@example.edu"})
        self.assertEqual(self.user("carol")["email"], "carol@example.edu")
        self.assertIn("carol@example.edu", admin.get("/admin/users").text)


class MailerTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {k: "" for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_SECURITY")})
        env.start()
        self.addCleanup(env.stop)

    def test_without_smtp_the_mail_goes_to_the_log(self):
        with self.assertLogs("uvicorn.error", level="WARNING") as logs:
            mailer.send("a@example.edu", "Subject", "the reset link")
        self.assertIn("the reset link", "\n".join(logs.output))

    def test_starttls_login_and_send(self):
        os.environ.update(SMTP_HOST="smtp.example.edu", SMTP_USERNAME="bot", SMTP_PASSWORD="secret", SMTP_FROM="RDW <bot@example.edu>")
        server = MagicMock()
        with patch("smtplib.SMTP", return_value=server) as smtp:
            mailer.send("a@example.edu", "Subject", "Body")
        smtp.assert_called_once_with("smtp.example.edu", 587, timeout=mailer.TIMEOUT_SECONDS)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("bot", "secret")
        message = server.send_message.call_args[0][0]
        self.assertEqual((message["To"], message["From"], message["Subject"]), ("a@example.edu", "RDW <bot@example.edu>", "Subject"))

    def test_ssl_mode_uses_port_465(self):
        os.environ.update(SMTP_HOST="smtp.example.edu", SMTP_SECURITY="ssl")
        with patch("smtplib.SMTP_SSL", return_value=MagicMock()) as smtp_ssl:
            mailer.send("a@example.edu", "S", "B")
        self.assertEqual(smtp_ssl.call_args[0][:2], ("smtp.example.edu", 465))

    def test_smtp_failure_becomes_mail_error(self):
        os.environ.update(SMTP_HOST="smtp.example.edu")
        with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
            with self.assertRaises(mailer.MailError):
                mailer.send("a@example.edu", "S", "B")


if __name__ == "__main__":
    unittest.main()


class SecretsCheckTests(unittest.TestCase):
    STRONG = {"SESSION_SECRET": "s" * 48, "ADMIN_PASSWORD": "a-real-password", "POSTGRES_PASSWORD": "p",
              "MONGO_INITDB_ROOT_PASSWORD": "m", "MINIO_ROOT_PASSWORD": "minio-pass"}

    def test_placeholders_refused_in_production_only(self):
        from app import bootstrap
        weak = {**self.STRONG, "ADMIN_PASSWORD": "REPLACE_WITH_STRONG_PASSWORD"}
        with patch.dict(os.environ, {**weak, "APP_BASE_URL": "https://rdw.example.edu"}):
            with self.assertRaises(SystemExit):
                bootstrap.check_secrets()
        with patch.dict(os.environ, {**weak, "APP_BASE_URL": "http://127.0.0.1:8001"}):
            bootstrap.check_secrets()  # local: warning only

    def test_short_session_secret_refused_in_production(self):
        from app import bootstrap
        with patch.dict(os.environ, {**self.STRONG, "SESSION_SECRET": "short", "APP_BASE_URL": "https://rdw.example.edu"}):
            with self.assertRaises(SystemExit):
                bootstrap.check_secrets()
        with patch.dict(os.environ, {**self.STRONG, "APP_BASE_URL": "https://rdw.example.edu"}):
            bootstrap.check_secrets()  # all good: no exit
