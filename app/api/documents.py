from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Query, UploadFile

from app.services import documents as service

router = APIRouter(tags=["documents"])


@router.post("/projects/{project_id}/documents", status_code=201)
def upload_document(
    project_id: UUID,
    file: Annotated[UploadFile, File()],
    tags: Annotated[str, Form(max_length=5000)] = "",
    authors: Annotated[str, Form(max_length=5000)] = "",
    custom_metadata: Annotated[str, Form(max_length=16000)] = "{}",
):
    return service.upload_document(project_id, file, tags, authors, custom_metadata)


@router.get("/projects/{project_id}/documents")
def list_documents(
    project_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return service.list_documents(project_id, limit, offset)


@router.get("/documents/{document_id}")
def get_document(document_id: UUID):
    return service.get_document(document_id)


@router.patch("/documents/{document_id}")
def update_document(
    document_id: UUID,
    tags: Annotated[str, Form(max_length=5000)] = "",
    authors: Annotated[str, Form(max_length=5000)] = "",
    custom_metadata: Annotated[str, Form(max_length=16000)] = "{}",
):
    return service.update_metadata(document_id, tags, authors, custom_metadata)


@router.get("/documents/{document_id}/download")
def download_document(document_id: UUID, range: Annotated[str | None, Header()] = None):
    """The original file as an attachment. Supports `Range` (resumable downloads)."""
    return service.download_document(document_id, range)


@router.get("/documents/{document_id}/content")
def preview_document(document_id: UUID, range: Annotated[str | None, Header()] = None):
    """Inline image/audio/video for previews; `Range` lets <video> seek. 415 for other kinds."""
    return service.preview_document(document_id, range)


@router.delete("/documents/{document_id}")
def delete_document(document_id: UUID):
    return service.delete_document(document_id)


@router.post("/documents/{document_id}/extract")
def extract_document(document_id: UUID):
    return service.extract_document(document_id)
