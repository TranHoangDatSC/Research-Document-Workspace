"""Upload/download orchestration; HTTP errors retained to preserve API behavior."""

import hashlib
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from fastapi.responses import Response

from app.bootstrap import bucket_name
from app.extractors import ExtractionError, extract_text
from app.repositories import documents as repository
from app.storage import minio_client

log = logging.getLogger("uvicorn.error")
MAX_BYTES = 10 * 1024 * 1024
MIME_TYPES = {
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def storage_error(stage, exc, document_id=None):
    log.warning(
        "document stage=%s id=%s error=%s", stage, document_id, type(exc).__name__
    )
    return HTTPException(503, "Document storage unavailable; check server logs")


def require_project(project_id):
    try:
        row = repository.project_exists(project_id)
    except psycopg.Error as exc:
        raise storage_error("read-project", exc) from None
    if row is None:
        raise HTTPException(404, "Project not found")


def document_row(document_id):
    try:
        row = repository.get_document(document_id)
    except psycopg.Error as exc:
        raise storage_error("read-document", exc, document_id) from None
    if row is None:
        raise HTTPException(404, "Document not found")
    return row


def require_ready(row):
    if row["status"] != "ready":
        raise HTTPException(409, "Document is not ready")


def split_values(value):
    result = list(
        dict.fromkeys(part.strip() for part in value.split(",") if part.strip())
    )
    if len(result) > 50 or any(len(part) > 200 for part in result):
        raise HTTPException(422, "At most 50 items, each at most 200 characters")
    return result


def compensate(document_id, object_name, object_attempted, mongo_attempted):
    # Only used before the final SQL commit is attempted. Do not delete a possibly
    # committed ready document if the result of that commit is unknown.
    if mongo_attempted:
        try:
            repository.delete_details(document_id)
        except Exception as exc:
            log.error(
                "cleanup MongoDB incomplete id=%s error=%s",
                document_id,
                type(exc).__name__,
            )
    if object_attempted:
        try:
            minio_client().remove_object(bucket_name(), object_name)
        except Exception as exc:
            log.error(
                "cleanup MinIO incomplete id=%s error=%s",
                document_id,
                type(exc).__name__,
            )
    try:
        repository.mark_failed(document_id)
    except Exception as exc:
        log.error(
            "cleanup PostgreSQL incomplete id=%s error=%s",
            document_id,
            type(exc).__name__,
        )


def upload_document(project_id, file, tags="", authors="", custom_metadata="{}"):
    require_project(project_id)
    filename = PurePosixPath((file.filename or "").replace("\\", "/")).name
    suffix = PurePosixPath(filename).suffix.lower()
    if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
        raise HTTPException(422, "Invalid filename")
    if suffix not in MIME_TYPES:
        raise HTTPException(415, "Allowed extensions: .txt, .pdf, .docx")
    try:
        metadata = json.loads(custom_metadata)
    except ValueError:
        raise HTTPException(422, "custom_metadata must be a JSON object") from None
    if not isinstance(metadata, dict):
        raise HTTPException(422, "custom_metadata must be a JSON object")
    tag_list, author_list = split_values(tags), split_values(authors)
    payload = file.file.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise HTTPException(413, "File exceeds 10 MiB")
    if not payload:
        raise HTTPException(422, "Empty file")

    document_id = uuid4()
    object_name = f"documents/{document_id}/original{suffix}"
    details = {
        "document_id": str(document_id),
        "tags": tag_list,
        "authors": author_list,
        "source": {"type": "upload", "url": None},
        "custom_metadata": metadata,
        "extracted_text": None,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    object_attempted = mongo_attempted = finalizing = False
    try:
        # Durable pending record makes interrupted uploads discoverable.
        repository.create_pending(
            document_id,
            project_id,
            filename,
            object_name,
            MIME_TYPES[suffix],
            len(payload),
        )
        object_attempted = True
        minio_client().put_object(
            bucket_name(),
            object_name,
            io.BytesIO(payload),
            len(payload),
            content_type=MIME_TYPES[suffix],
        )
        mongo_attempted = True
        repository.insert_details(details)
        finalizing = True
        row = repository.mark_ready(document_id)
    except Exception as exc:
        if finalizing:
            # SQL COMMIT could have succeeded even if its acknowledgement was lost.
            # Retain assets; operator can reconcile using the durable document ID.
            log.error("finalization uncertain; retain assets id=%s", document_id)
        else:
            compensate(document_id, object_name, object_attempted, mongo_attempted)
        raise storage_error("upload", exc, document_id) from None
    log.info("document_uploaded document_id=%s project_id=%s size_bytes=%s", document_id, project_id, len(payload))
    return {**row, **details}


def list_documents(project_id, limit=20, offset=0):
    require_project(project_id)
    try:
        return repository.list_documents(project_id, limit, offset)
    except psycopg.Error as exc:
        raise storage_error("list-documents", exc) from None


def get_document(document_id):
    row = document_row(document_id)
    require_ready(row)
    try:
        details = repository.get_details(document_id)
        if details is None:
            raise RuntimeError("Missing document metadata")
    except Exception as exc:
        raise storage_error("read-metadata", exc, document_id) from None
    return {**row, **details}


def read_object(row, stage):
    try:
        response = minio_client().get_object(bucket_name(), row["object_name"])
        try:
            # Bounded buffering avoids returning HTTP 200 before a failed read.
            payload = response.read(MAX_BYTES + 1)
        finally:
            response.close()
            response.release_conn()
        if len(payload) > MAX_BYTES or len(payload) != row["size_bytes"]:
            raise RuntimeError("Stored file size mismatch")
    except Exception as exc:
        raise storage_error(stage, exc, row["id"]) from None
    return payload


def download_document(document_id):
    row = document_row(document_id)
    require_ready(row)
    payload = read_object(row, "download")
    log.info("document_downloaded document_id=%s size_bytes=%s", document_id, len(payload))
    return Response(
        content=payload,
        media_type=row["content_type"],
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(row["original_name"], safe=""),
            "X-Content-Type-Options": "nosniff",
        },
    )


def delete_document(document_id):
    """Retryable deletion: durable intent -> MinIO -> MongoDB -> PostgreSQL.

    Missing rows are a successful no-op. Never delete a pending upload. A
    failed/uncertain external operation leaves the deleting row for retry.
    No cross-store atomicity is claimed; readers reject deleting documents.
    """
    try:
        row = repository.begin_delete(document_id)
        if row is None:
            current = repository.get_document(document_id)
            if current is not None:
                raise HTTPException(409, "Upload is pending; deletion is not allowed yet")
            return {"document_id": str(document_id), "deleted": True}
        minio_client().remove_object(bucket_name(), row["object_name"])
        repository.delete_details(document_id)
        repository.finish_delete(document_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise storage_error("delete-retry-required", exc, document_id) from None
    log.info("document_deleted document_id=%s", document_id)
    return {"document_id": str(document_id), "deleted": True}


def update_metadata(document_id, tags="", authors="", custom_metadata="{}"):
    row = document_row(document_id)
    require_ready(row)
    try:
        metadata = json.loads(custom_metadata)
    except ValueError:
        raise HTTPException(422, "custom_metadata must be a JSON object") from None
    if not isinstance(metadata, dict):
        raise HTTPException(422, "custom_metadata must be a JSON object")
    tag_list, author_list = split_values(tags), split_values(authors)
    try:
        repository.update_details(document_id, tag_list, author_list, metadata)
    except Exception as exc:
        raise storage_error("update-metadata", exc, document_id) from None
    log.info("document_metadata_updated document_id=%s", document_id)
    return get_document(document_id)


def extract_document(document_id):
    """Re-runnable: downloads the stored file, extracts text, overwrites MongoDB."""
    row = document_row(document_id)
    require_ready(row)
    payload = read_object(row, "extract-download")
    suffix = PurePosixPath(row["object_name"]).suffix.lower()
    try:
        result = extract_text(payload, suffix)
    except ExtractionError as exc:
        log.info("extraction_rejected document_id=%s code=%s", document_id, exc.code)
        raise HTTPException(422, f"Text extraction failed: {exc.code}") from None

    extracted = {
        "text": result.text,
        "method": result.method,
        "character_count": result.character_count,
        "word_count": result.word_count,
        "truncated": result.truncated,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        repository.update_extracted_text(document_id, extracted)
    except Exception as exc:
        raise storage_error("extract-persist", exc, document_id) from None

    log.info(
        "document_extracted document_id=%s method=%s characters=%s truncated=%s",
        document_id, result.method, result.character_count, result.truncated,
    )
    return get_document(document_id)
