"""Session check cached in Redis: hits skip PostgreSQL, revoking actions take
effect at once, and Redis failures fall back to the database."""
import json
import unittest
from unittest.mock import patch

from app import cache
from app.repositories import users as users_repo
from app.services import auth as auth_service


class FakeRedis:
    def __init__(self):
        self.values, self.ttl = {}, {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key], self.ttl[key] = value.encode(), ex

    def delete(self, key):
        self.values.pop(key, None)


class BrokenRedis:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ConnectionError("redis is down")
        return fail


class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        self.row = {"id": "u1", "username": "dat", "role": "user", "is_active": True, "session_version": 3}
        self.db_reads = 0

        def get_by_id(user_id, with_password=False):
            self.db_reads += 1
            return dict(self.row) if user_id == self.row["id"] else None

        def set_active(user_id, is_active):
            self.row["is_active"] = is_active
            self.row["session_version"] += 0 if is_active else 1
            return dict(self.row)

        def set_role(user_id, role):
            self.row["role"] = role
            self.row["session_version"] += 1
            return dict(self.row)

        for name, fake in (("get_by_id", get_by_id), ("set_active", set_active), ("set_role", set_role)):
            patcher = patch.object(users_repo, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        cache._last_warning = 0.0

    def use(self, client):
        patcher = patch.object(cache, "redis_client", lambda: client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    @staticmethod
    def session(version=3):
        return {"user_id": "u1", "username": "dat", "role": "user", "session_version": version}

    def test_second_check_is_served_from_redis(self):
        fake = self.use(FakeRedis())
        first = auth_service.session_user(self.session())
        second = auth_service.session_user(self.session())
        self.assertEqual(first, second)
        self.assertEqual(first, {"user_id": "u1", "username": "dat", "role": "user"})
        self.assertEqual(self.db_reads, 1)
        self.assertEqual(fake.ttl["session:u1"], auth_service.SESSION_CACHE_SECONDS)
        self.assertEqual(json.loads(fake.values["session:u1"])["version"], 3)

    def test_locking_ends_a_cached_session_at_once(self):
        self.use(FakeRedis())
        auth_service.session_user(self.session())  # now cached
        auth_service.set_active("u1", False, current_user_id="admin")
        self.assertIsNone(auth_service.session_user(self.session()))

    def test_demotion_ends_a_cached_session_at_once(self):
        self.use(FakeRedis())
        auth_service.session_user(self.session())
        auth_service.set_role("u1", "admin", current_user_id="admin")
        self.assertIsNone(auth_service.session_user(self.session(version=3)))
        self.assertEqual(auth_service.session_user(self.session(version=4))["role"], "admin")

    def test_old_cookie_never_matches_a_newer_cached_version(self):
        self.use(FakeRedis())
        self.row["session_version"] = 4
        auth_service.session_user(self.session(version=4))  # cache holds version 4
        self.assertIsNone(auth_service.session_user(self.session(version=3)))

    def test_invalid_session_is_not_cached(self):
        fake = self.use(FakeRedis())
        self.row["is_active"] = False
        self.assertIsNone(auth_service.session_user(self.session()))
        self.assertEqual(fake.values, {})

    def test_redis_down_falls_back_to_database(self):
        self.use(BrokenRedis())
        with self.assertLogs("uvicorn.error", "WARNING") as logs:
            user = auth_service.session_user(self.session())
            auth_service.session_user(self.session())
        self.assertEqual(user["user_id"], "u1")
        self.assertEqual(self.db_reads, 2)
        warnings = [line for line in logs.output if "cache_redis_unavailable" in line]
        self.assertEqual(len(warnings), 1)  # throttled, not one per request

    def test_without_redis_every_check_reads_the_database(self):
        self.use(None)
        auth_service.session_user(self.session())
        auth_service.session_user(self.session())
        self.assertEqual(self.db_reads, 2)


if __name__ == "__main__":
    unittest.main()
