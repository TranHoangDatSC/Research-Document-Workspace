"""Shared login helper for integration tests against the local stack.

Reads ADMIN_USERNAME/ADMIN_PASSWORD from .env — the same credentials
app/bootstrap.py uses to seed the first admin account on a fresh database.
Not a standalone test (leading underscore); imported by day2/day3/day5.
"""
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read_env(key):
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    raise RuntimeError(f"{key} not found in .env")


def build_cookie_opener(*extra_handlers):
    """A urllib opener that remembers cookies across requests, for a login session."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), *extra_handlers)


def login(base_url, opener=None, username=None, password=None):
    data = urllib.parse.urlencode({
        "username": username or read_env("ADMIN_USERNAME"),
        "password": password or read_env("ADMIN_PASSWORD"),
    }).encode()
    req = urllib.request.Request(
        base_url + "/login", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener_open = opener.open if opener is not None else urllib.request.urlopen
    # A NoRedirect-style opener makes urllib raise HTTPError for the 303
    # response instead of returning it — accept either shape here.
    try:
        with opener_open(req, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status not in (200, 303):
        raise RuntimeError(f"Login failed: HTTP {status}")
