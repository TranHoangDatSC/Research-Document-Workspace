"""app/settings.py: the env/Postgres bridge behind /admin/settings. Every
reader elsewhere (llm.py, app/domains/, media_ai.py) just reads os.environ
as always — this is what mutates it, so these tests check os.environ
directly rather than some settings-specific getter.
"""
import os
import unittest
from unittest.mock import patch

import psycopg

from app import settings


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        self.mock_get = patch.object(settings.repository, "get_setting", return_value=None).start()
        self.mock_set = patch.object(settings.repository, "set_setting").start()
        self.addCleanup(patch.stopall)
        # Isolate os.environ and _ORIGINAL_ENV for the two keys under test,
        # so nothing here can leak into another test file.
        self._env_patch = patch.dict(os.environ, {"LLM_MODEL": "env-default-model"})
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        os.environ.pop("LLM_API_KEY", None)
        self._original_patch = patch.dict(settings._ORIGINAL_ENV, {"LLM_MODEL": "env-default-model", "LLM_API_KEY": None})
        self._original_patch.start()
        self.addCleanup(self._original_patch.stop)


class EffectiveAndOverrideTests(SettingsTestCase):
    def test_effective_reads_os_environ(self):
        os.environ["LLM_MODEL"] = "something-else"
        self.assertEqual(settings.effective("LLM_MODEL"), "something-else")

    def test_missing_key_is_empty_string_not_keyerror(self):
        self.assertEqual(settings.effective("LLM_API_KEY"), "")

    def test_not_overridden_when_equal_to_original(self):
        os.environ["LLM_MODEL"] = "env-default-model"
        self.assertFalse(settings.is_overridden("LLM_MODEL"))

    def test_overridden_once_changed(self):
        os.environ["LLM_MODEL"] = "changed-model"
        self.assertTrue(settings.is_overridden("LLM_MODEL"))


class UpdateTests(SettingsTestCase):
    def test_persists_and_applies_immediately(self):
        settings.update("LLM_MODEL", "new-model", "admin-1")
        self.assertEqual(os.environ["LLM_MODEL"], "new-model")
        self.mock_set.assert_called_once_with("LLM_MODEL", "new-model", "admin-1")

    def test_strips_whitespace(self):
        settings.update("LLM_MODEL", "  padded-model  ", "admin-1")
        self.assertEqual(os.environ["LLM_MODEL"], "padded-model")

    def test_blank_restores_the_value_this_process_started_with(self):
        os.environ["LLM_MODEL"] = "some-override"
        settings.update("LLM_MODEL", "", "admin-1")
        self.assertEqual(os.environ["LLM_MODEL"], "env-default-model")

    def test_blank_pops_a_key_that_had_no_original_value(self):
        os.environ["LLM_API_KEY"] = "some-override-key"
        settings.update("LLM_API_KEY", "", "admin-1")
        self.assertNotIn("LLM_API_KEY", os.environ)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ValueError):
            settings.update("NOT_A_REAL_SETTING", "x", "admin-1")

    def test_write_failure_raises_http_503_and_leaves_environ_untouched(self):
        from fastapi import HTTPException
        self.mock_set.side_effect = psycopg.Error("down")
        os.environ["LLM_MODEL"] = "unchanged"
        with self.assertRaises(HTTPException) as caught:
            settings.update("LLM_MODEL", "new-model", "admin-1")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(os.environ["LLM_MODEL"], "unchanged")

    def test_never_logs_the_secret_value(self):
        with self.assertLogs("uvicorn.error", level="INFO") as logs:
            settings.update("LLM_API_KEY", "super-secret-value", "admin-1")
        self.assertTrue(any("key=LLM_API_KEY" in m for m in logs.output))
        self.assertFalse(any("super-secret-value" in m for m in logs.output))


class MaskedTests(SettingsTestCase):
    def test_empty_is_none(self):
        self.assertIsNone(settings.masked("LLM_API_KEY"))

    def test_short_value_fully_masked(self):
        os.environ["LLM_API_KEY"] = "abc"
        self.assertEqual(settings.masked("LLM_API_KEY"), "••••")

    def test_long_value_shows_only_last_four(self):
        os.environ["LLM_API_KEY"] = "sk-abcdef1234"
        value = settings.masked("LLM_API_KEY")
        self.assertTrue(value.endswith("1234"))
        self.assertNotIn("abcdef", value)

    def test_key_list_shows_a_count_not_any_fragment(self):
        os.environ["LLM_API_KEYS"] = "key-one,key-two, key-three"
        self.assertEqual(settings.masked("LLM_API_KEYS"), "Đang lưu 3 khóa")


class ApplySavedOverridesTests(SettingsTestCase):
    def test_pulls_db_rows_into_os_environ(self):
        self.mock_get.side_effect = lambda key: "db-value" if key == "LLM_MODEL" else None
        settings.apply_saved_overrides()
        self.assertEqual(os.environ["LLM_MODEL"], "db-value")

    def test_a_database_error_is_swallowed(self):
        self.mock_get.side_effect = psycopg.Error("down")
        settings.apply_saved_overrides()  # must not raise
