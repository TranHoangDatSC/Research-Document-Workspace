"""Rate limiter on Redis: shared counts, and the in-memory fallback when Redis fails."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import ratelimit
from app.api import health
from app.main import app
from tests.unit.support import FakeBackend


class FakeRedis:
    """The sorted-set subset ratelimit uses, with redis-py's pipeline shape."""

    def __init__(self):
        self.sets = {}
        self.expiry = {}

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def zremrangebyscore(self, key, low, high):
        self.ops.append(lambda s: len([s.pop(m) for m, v in list(s.items()) if low <= v <= high]))
        self.key = key

    def zcard(self, key):
        self.ops.append(lambda s: len(s))

    def zrange(self, key, start, stop, withscores=False):
        self.ops.append(lambda s: sorted(s.items(), key=lambda item: item[1])[start:stop + 1])

    def zadd(self, key, mapping):
        self.key = key
        self.ops.append(lambda s: s.update(mapping) or len(mapping))

    def expire(self, key, seconds):
        self.ops.append(lambda s: self.redis.expiry.__setitem__(key, seconds) or True)

    def execute(self):
        members = self.redis.sets.setdefault(self.key, {})
        return [op(members) for op in self.ops]


class BrokenRedis:
    def pipeline(self):
        raise ConnectionError("redis is down")


def request(ip="203.0.113.7"):
    return SimpleNamespace(headers={}, client=SimpleNamespace(host=ip))


class RedisRateLimitTests(unittest.TestCase):
    def setUp(self):
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)

    def use(self, client):
        patcher = patch.object(ratelimit, "redis_client", lambda: client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def exhaust(self, bucket="login", ip="203.0.113.7"):
        limit, _ = ratelimit.LIMITS[bucket]
        for _ in range(limit):
            ratelimit.check(bucket, request(ip))

    def test_limit_reached_in_redis_returns_429_with_retry_after(self):
        fake = FakeRedis()
        self.use(fake)
        self.exhaust()
        with self.assertRaises(HTTPException) as caught:
            ratelimit.check("login", request())
        self.assertEqual(caught.exception.status_code, 429)
        self.assertGreater(int(caught.exception.headers["Retry-After"]), 0)
        self.assertEqual(fake.expiry["rl:login:203.0.113.7"], 5 * 60)

    def test_counts_survive_a_web_restart(self):
        self.use(FakeRedis())
        self.exhaust()
        ratelimit.reset()  # a restarted process starts with empty memory
        with self.assertRaises(HTTPException):
            ratelimit.check("login", request())

    def test_counts_are_per_client(self):
        self.use(FakeRedis())
        self.exhaust(ip="203.0.113.7")
        ratelimit.check("login", request("198.51.100.1"))  # a different IP is unaffected

    def test_redis_down_falls_back_to_memory_and_still_limits(self):
        self.use(BrokenRedis())
        with self.assertLogs("uvicorn.error", "WARNING") as logs:
            self.exhaust()
            with self.assertRaises(HTTPException) as caught:
                ratelimit.check("login", request())
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("ratelimit_redis_unavailable", logs.output[0])

    def test_without_redis_url_counts_in_memory(self):
        self.use(None)
        self.exhaust()
        with self.assertRaises(HTTPException):
            ratelimit.check("login", request())


class RedisHealthTests(unittest.TestCase):
    def setUp(self):
        FakeBackend().install(self)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_redis_down_is_reported_but_app_stays_ready(self):
        def down():
            raise ConnectionError("redis is down")

        with patch.object(health, "check_redis", down):
            response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["services"]["redis"], "down")

    def test_redis_disabled_without_redis_url(self):
        response = self.client.get("/health/ready")
        self.assertEqual(response.json()["services"]["redis"], "disabled")


if __name__ == "__main__":
    unittest.main()
