"""Who is making the current request — for per-user data isolation.

The login middleware (app/main.py) binds the session user here for the whole
request; services read it with `user_id()` to scope every project/document
query to its owner. Fails closed: a service called with nobody bound gets a
401 instead of silently seeing everyone's data.

A ContextVar rather than a parameter on every service function: it reaches
the sync endpoints FastAPI runs in a worker thread (anyio copies the context),
and a new code path cannot forget to pass it along.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException

_current_user = ContextVar("current_user", default=None)


def bind(user):
    """user: the session dict {"user_id", "username", "role"}. Returns a reset token."""
    return _current_user.set(user)


def unbind(token):
    _current_user.reset(token)


def user_id():
    user = _current_user.get()
    if user is None:
        raise HTTPException(401, "Not authenticated")
    return user["user_id"]


@contextmanager
def acting_as(user_id, username="system", role="user"):
    """For scripts and tests that call services outside an HTTP request."""
    token = bind({"user_id": str(user_id), "username": username, "role": role})
    try:
        yield
    finally:
        unbind(token)
