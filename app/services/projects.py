"""Project use cases, shared by the JSON API and the HTML UI."""
import logging
import psycopg
from fastapi import HTTPException
from app import access
from app.repositories import chats as chats_repository
from app.repositories import documents as documents_repository
from app.repositories import projects as repository

log = logging.getLogger("uvicorn.error")

def call(fn, *args):
    try:
        return fn(*args)
    except psycopg.Error as exc:
        log.warning("project_storage_failed error=%s", type(exc).__name__)
        raise HTTPException(503, "Project storage is temporarily unavailable") from None

def create_project(payload):
    row = call(repository.create_project, payload, access.user_id())
    log.info("project_created project_id=%s", row["id"])
    return row

def list_projects(limit=20, offset=0, query=None):
    return call(repository.list_projects, limit, offset, query, access.user_id())

def count_projects(query=None):
    return call(repository.count_projects, query, access.user_id())

def get_project(project_id):
    row = call(repository.get_project, project_id, access.user_id())
    if row is None:
        raise HTTPException(404, "Project not found")
    return row

def update_project(project_id, payload):
    get_project(project_id)
    row = call(repository.update_project, project_id, payload.name, payload.description, access.user_id())
    log.info("project_updated project_id=%s", project_id)
    return row

def delete_project(project_id):
    # Local import keeps services.projects -> services.documents one-way.
    from app.services import documents as documents_service

    get_project(project_id)
    docs = call(documents_repository.list_all_documents, project_id)
    if any(d["status"] == "pending" for d in docs):
        raise HTTPException(409, "Dự án có tài liệu đang tải lên; đợi hoàn tất hoặc thất bại rồi thử lại.")
    for doc in docs:
        documents_service.delete_document(doc["id"])
    try:
        chats_repository.delete_project(project_id)
    except Exception as exc:
        # Leftover chat messages are unreachable; don't block the delete.
        log.warning("chat_storage_failed stage=delete-project error=%s", type(exc).__name__)
    call(repository.delete_project, project_id, access.user_id())
    log.info("project_deleted project_id=%s document_count=%s", project_id, len(docs))
