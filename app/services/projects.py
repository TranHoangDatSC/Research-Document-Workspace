"""Shared project operations for JSON API and HTML forms."""
import logging
import psycopg
from fastapi import HTTPException
from app.repositories import projects as repository

log = logging.getLogger("uvicorn.error")

def call(fn, *args):
    try:
        return fn(*args)
    except psycopg.Error as exc:
        log.warning("project_storage_failed error=%s", type(exc).__name__)
        raise HTTPException(503, "Project storage is temporarily unavailable") from None

def create_project(payload):
    row = call(repository.create_project, payload)
    log.info("project_created project_id=%s", row["id"])
    return row

def list_projects(limit=20, offset=0):
    return call(repository.list_projects, limit, offset)

def count_projects():
    return call(repository.count_projects)

def get_project(project_id):
    row = call(repository.get_project, project_id)
    if row is None:
        raise HTTPException(404, "Project not found")
    return row
