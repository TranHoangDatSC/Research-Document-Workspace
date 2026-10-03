"""Admin-only pages added for technical/AI settings and AI usage stats
(app/ui/admin.py's /admin/settings and /admin/stats), end to end through
FakeBackend — same style as the existing /admin/users tests.
"""
import os
import unittest

from support import FakeBackend

from app import settings


class AdminSettingsTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.admin = self.backend.client(self, role="admin", username="root")
        self._env_before = {key: os.environ.get(key) for key in settings.KEYS}
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_non_admin_is_forbidden(self):
        user = self.backend.client(self, role="user", username="bob")
        self.assertEqual(user.get("/admin/settings").status_code, 403)
        self.assertEqual(user.post("/admin/settings", data={"key": "LLM_MODEL", "value": "x"}).status_code, 403)

    def test_page_renders_for_admin(self):
        self.assertEqual(self.admin.get("/admin/settings").status_code, 200)

    def test_saved_secret_is_never_echoed_back_to_the_browser(self):
        settings.update("LLM_API_KEY", "super-secret-key-value", "admin-1")
        html = self.admin.get("/admin/settings").text
        self.assertNotIn("super-secret-key-value", html)

    def test_updating_a_plain_setting_takes_effect_immediately_and_persists(self):
        response = self.admin.post("/admin/settings", data={"key": "LLM_MODEL", "value": "gemini-super"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(settings.effective("LLM_MODEL"), "gemini-super")
        self.assertEqual(self.backend.settings["LLM_MODEL"], "gemini-super")

    def test_blank_secret_submit_does_not_overwrite_the_existing_key(self):
        settings.update("LLM_API_KEY", "original-key", "admin-1")
        self.admin.post("/admin/settings", data={"key": "LLM_API_KEY", "value": ""}, follow_redirects=False)
        self.assertEqual(settings.effective("LLM_API_KEY"), "original-key")

    def test_blank_non_secret_clears_the_override(self):
        original = settings._ORIGINAL_ENV.get("LLM_MODEL")
        settings.update("LLM_MODEL", "temporary-override", "admin-1")
        self.assertEqual(settings.effective("LLM_MODEL"), "temporary-override")
        self.admin.post("/admin/settings", data={"key": "LLM_MODEL", "value": ""}, follow_redirects=False)
        if original:
            self.assertEqual(settings.effective("LLM_MODEL"), original)
        else:
            self.assertNotIn("LLM_MODEL", os.environ)

    def test_unknown_key_is_422(self):
        self.assertEqual(self.admin.post("/admin/settings", data={"key": "NOT_REAL", "value": "x"}).status_code, 422)


class AdminStatsTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.admin = self.backend.client(self, role="admin", username="root")

    def test_non_admin_is_forbidden(self):
        user = self.backend.client(self, role="user", username="bob")
        self.assertEqual(user.get("/admin/stats").status_code, 403)

    def test_empty_state_renders(self):
        response = self.admin.get("/admin/stats")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Chưa có lượt gọi AI nào", response.text)

    def test_shows_recorded_usage_across_sources(self):
        self.backend.record_llm_usage("ask", "gemini", "gemini-flash-latest", True, 400, usage={"input": 120, "output": 40})
        self.backend.record_llm_usage("graph", "gemini", "gemini-flash-latest", False, 200, error="HTTP 429")
        response = self.admin.get("/admin/stats?range=year")
        self.assertEqual(response.status_code, 200)
        self.assertIn("gemini-flash-latest", response.text)

    def test_unknown_range_falls_back_without_error(self):
        self.assertEqual(self.admin.get("/admin/stats?range=nope").status_code, 200)
