"""Runtime settings shared between uvicorn workers through Redis pub/sub."""
import os
import unittest
from unittest.mock import patch

from app import settings


class FakeRedis:
    def __init__(self, messages=()):
        self.published = []
        self.messages = list(messages)
        self.subscribe_failures = 0

    def publish(self, channel, message):
        self.published.append((channel, message))

    def pubsub(self, ignore_subscribe_messages=False):
        return FakePubSub(self)


class FakePubSub:
    def __init__(self, redis):
        self.redis = redis

    def subscribe(self, channel):
        if self.redis.subscribe_failures:
            self.redis.subscribe_failures -= 1
            raise ConnectionError("redis is down")

    def get_message(self, timeout=None):
        if self.redis.messages:
            return self.redis.messages.pop(0)
        settings._stop.set()  # nothing left: end the listener loop
        return None

    def close(self):
        pass


class BrokenRedis:
    def publish(self, channel, message):
        raise ConnectionError("redis is down")


class SettingsSyncTests(unittest.TestCase):
    def setUp(self):
        self.db = {}
        patches = (
            patch.dict(os.environ, {"LLM_MODEL": "env-model"}),
            patch.dict(settings._ORIGINAL_ENV, {"LLM_MODEL": "env-model", "LLM_BASE_URL": None}),
            patch.object(settings.repository, "get_setting", lambda key: self.db.get(key)),
            patch.object(settings.repository, "set_setting", lambda key, value, admin: self.db.__setitem__(key, value)),
            patch.object(settings, "RECONNECT_SECONDS", 0),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        settings._stop.clear()
        self.addCleanup(settings._stop.clear)

    def use(self, client):
        p = patch.object(settings, "redis_client", lambda: client)
        p.start()
        self.addCleanup(p.stop)
        return client

    def test_update_publishes_the_key_but_never_the_value(self):
        fake = self.use(FakeRedis())
        settings.update("LLM_API_KEY", "secret-value", "admin-1")
        self.assertEqual(fake.published, [(settings.CHANNEL, "LLM_API_KEY")])
        self.assertNotIn("secret-value", repr(fake.published))

    def test_update_still_saves_when_redis_is_down(self):
        self.use(BrokenRedis())
        with self.assertLogs("uvicorn.error", "WARNING") as logs:
            settings.update("LLM_MODEL", "new-model", "admin-1")
        self.assertEqual(os.environ["LLM_MODEL"], "new-model")
        self.assertEqual(self.db["LLM_MODEL"], "new-model")
        self.assertIn("settings_publish_failed", logs.output[0])

    def test_message_from_another_worker_applies_the_saved_value(self):
        self.db["LLM_MODEL"] = "changed-elsewhere"
        settings.handle_message({"type": "message", "data": b"LLM_MODEL"})
        self.assertEqual(os.environ["LLM_MODEL"], "changed-elsewhere")

    def test_cleared_elsewhere_restores_the_env_value(self):
        os.environ["LLM_MODEL"] = "old-override"
        self.db["LLM_MODEL"] = ""
        settings.handle_message({"type": "message", "data": "LLM_MODEL"})
        self.assertEqual(os.environ["LLM_MODEL"], "env-model")

    def test_unknown_keys_and_non_messages_are_ignored(self):
        settings.handle_message({"type": "message", "data": b"PATH"})
        settings.handle_message({"type": "subscribe", "data": 1})
        settings.handle_message(None)
        self.assertEqual(os.environ["LLM_MODEL"], "env-model")

    def test_listener_applies_published_changes(self):
        self.db["LLM_MODEL"] = "from-pubsub"
        self.use(FakeRedis([{"type": "message", "data": b"LLM_MODEL"}]))
        settings._listen()
        self.assertEqual(os.environ["LLM_MODEL"], "from-pubsub")

    def test_listener_reconnects_and_catches_up(self):
        fake = self.use(FakeRedis())
        fake.subscribe_failures = 1
        self.db["LLM_MODEL"] = "missed-while-down"
        with self.assertLogs("uvicorn.error", "WARNING") as logs:
            settings._listen()
        self.assertIn("settings_listener_disconnected", logs.output[0])
        self.assertEqual(os.environ["LLM_MODEL"], "missed-while-down")  # full reload on reconnect

    def test_no_listener_without_redis(self):
        self.use(None)
        self.assertIsNone(settings.start_listener())


if __name__ == "__main__":
    unittest.main()
