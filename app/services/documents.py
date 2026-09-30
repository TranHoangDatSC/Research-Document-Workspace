"""Upload/download orchestration; HTTP errors retained to preserve API behavior."""

import hashlib
import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app import file_types
from app.bootstrap import bucket_name
from app.extractors import MAX_INPUT_BYTES as EXTRACT_MAX_BYTES, ExtractionError, extract_text
from app.repositories import documents as repository
from app.storage import minio_client

log = logging.getLogger("uvicorn.error")
# MinIO multipart part size. minio-py keeps at most 3 parts uploading plus 1
# being read, so one upload holds ~4 x 8 MiB in memory however large the file.
UPLOAD_PART_SIZE = 8 * 1024 * 1024
# Streamed to the client in chunks of this size (download / preview).
STREAM_CHUNK = 256 * 1024


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


class _HashingReader:
    """Wraps the upload's temp file: MinIO pulls from it part by part and the
    SHA-256 is computed on the way through — the file is never all in memory."""

    def __init__(self, raw):
        self.raw = raw
        self.sha256 = hashlib.sha256()

    def read(self, size=-1):
        chunk = self.raw.read(size)
        self.sha256.update(chunk)
        return chunk


def _file_size(upload):
    # Starlette has already spooled the upload to a temp file; measure it there.
    upload.file.seek(0, io.SEEK_END)
    size = upload.file.tell()
    upload.file.seek(0)
    return size


def upload_document(project_id, file, tags="", authors="", custom_metadata="{}"):
    require_project(project_id)
    filename = PurePosixPath((file.filename or "").replace("\\", "/")).name
    suffix = file_types.suffix_of(filename)
    if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
        raise HTTPException(422, "Invalid filename")
    kind = file_types.kind_of(filename)
    if kind is None:
        raise HTTPException(415, "Unsupported file type. Allowed: " + " ".join(file_types.EXTENSIONS))
    try:
        metadata = json.loads(custom_metadata)
    except ValueError:
        raise HTTPException(422, "custom_metadata must be a JSON object") from None
    if not isinstance(metadata, dict):
        raise HTTPException(422, "custom_metadata must be a JSON object")
    tag_list, author_list = split_values(tags), split_values(authors)
    size = _file_size(file)
    limit = file_types.limit_bytes(kind)
    if size > limit:
        raise HTTPException(413, f"{kind.label} tối đa {limit // (1024 * 1024)} MiB")
    if size == 0:
        raise HTTPException(422, "Empty file")

    document_id = uuid4()
    object_name = f"documents/{document_id}/original{suffix}"
    content_type = file_types.mime_of(filename)
    reader = _HashingReader(file.file)
    object_attempted = mongo_attempted = finalizing = False
    try:
        # Durable pending record makes interrupted uploads discoverable.
        repository.create_pending(document_id, project_id, filename, object_name, content_type, size)
        object_attempted = True
        minio_client().put_object(
            bucket_name(), object_name, reader, size,
            content_type=content_type, part_size=UPLOAD_PART_SIZE,
        )
        details = {
            "document_id": str(document_id),
            "tags": tag_list,
            "authors": author_list,
            "source": {"type": "upload", "url": None},
            "custom_metadata": metadata,
            "extracted_text": None,
            "sha256": reader.sha256.hexdigest(),
        }
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
    log.info("document_uploaded document_id=%s project_id=%s kind=%s size_bytes=%s", document_id, project_id, kind.name, size)
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
    """Whole file in memory — only for text extraction, which is capped at
    EXTRACT_MAX_BYTES (checked before calling). Downloads stream instead."""
    try:
        response = minio_client().get_object(bucket_name(), row["object_name"])
        try:
            payload = response.read(EXTRACT_MAX_BYTES + 1)
        finally:
            response.close()
            response.release_conn()
        if len(payload) != row["size_bytes"]:
            raise RuntimeError("Stored file size mismatch")
    except Exception as exc:
        raise storage_error(stage, exc, row["id"]) from None
    return payload


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def parse_range(header, size):
    """HTTP Range -> (start, end) inclusive, or None for the whole file.
    Malformed or multi-range headers are ignored (whole file, 200), as the
    spec allows; a range entirely past the end is 416."""
    match = _RANGE_RE.fullmatch((header or "").strip())
    if not match or match.group(1) == match.group(2) == "":
        return None
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:  # "bytes=-N": the last N bytes
        start, end = max(size - int(last), 0), size - 1
        if int(last) == 0:
            start = size
    if start > end or start >= size:
        raise HTTPException(416, "Requested range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
    return start, end


def _stream_object(row, range_header, disposition, stage, extra_headers=None):
    """Streams the stored file (or the requested byte range) from MinIO in
    STREAM_CHUNK pieces, so a 500 MiB video never sits in server memory and
    <video> can seek. A failure mid-stream can only cut the response short
    (the 200/206 is already sent); the client sees an incomplete file."""
    size = row["size_bytes"]
    byte_range = parse_range(range_header, size)
    start, end = byte_range or (0, size - 1)
    try:
        response = minio_client().get_object(bucket_name(), row["object_name"], offset=start, length=end - start + 1)
    except Exception as exc:
        raise storage_error(stage, exc, row["id"]) from None

    def body():
        try:
            yield from response.stream(STREAM_CHUNK)
        finally:
            response.close()
            response.release_conn()

    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''" + quote(row["original_name"], safe=""),
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        # A document's file never changes (a new upload is a new document).
        "Cache-Control": "private, max-age=86400",
        **(extra_headers or {}),
    }
    if byte_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(body(), status_code=206 if byte_range else 200, media_type=row["content_type"], headers=headers)


def download_document(document_id, range_header=None):
    row = document_row(document_id)
    require_ready(row)
    log.info("document_downloaded document_id=%s size_bytes=%s", document_id, row["size_bytes"])
    return _stream_object(row, range_header, "attachment", "download")


def preview_document(document_id, range_header=None):
    """Inline for <img>/<audio>/<video> on the document page. Only media kinds:
    everything else is download-only. `sandbox` keeps even a mislabelled file
    from running script if opened directly."""
    row = document_row(document_id)
    require_ready(row)
    kind = file_types.kind_of(row["object_name"])
    if kind is None or kind.preview is None:
        raise HTTPException(415, "Loại tệp này không xem trước được, hãy tải về")
    return _stream_object(row, range_header, "inline", "preview", {"Content-Security-Policy": "sandbox"})


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
    # Decided before downloading anything: no point pulling a 500 MiB video
    # out of MinIO to find out it has no text.
    kind = file_types.kind_of(row["object_name"])
    if kind is None or not kind.extractable:
        raise HTTPException(422, f"{kind.label if kind else 'Loại tệp này'} không có văn bản để trích xuất")
    if row["size_bytes"] > EXTRACT_MAX_BYTES:
        raise HTTPException(422, f"Tệp quá {EXTRACT_MAX_BYTES // (1024 * 1024)} MiB, không trích xuất văn bản được (vẫn tải về và lưu bình thường)")
    payload = read_object(row, "extract-download")
    suffix = file_types.suffix_of(row["object_name"])
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
