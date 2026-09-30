"""Which files can be stored, how they are grouped, and how big they may be.

The single source of truth for the upload allowlist (services/documents.py),
the file picker and size check in the browser (templates + app.js), previews
and whether a file has text to extract for AI questions.

Only the extension is checked, never the content: stored files are always
served with their allowlisted MIME type and `X-Content-Type-Options: nosniff`,
so a renamed file can't be run as something else by the browser.

Size limits per kind, in MiB, can be raised or lowered without a code change
via `MAX_UPLOAD_MB_<KIND>` (e.g. MAX_UPLOAD_MB_VIDEO=1000). Uploads are
streamed to MinIO, so a limit costs disk space, not server memory.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Kind:
    name: str
    label: str
    icon: str
    default_limit_mb: int
    extractable: bool = False  # has text for "Trích xuất văn bản" and AI questions
    preview: str | None = None  # "image" | "audio" | "video": shown inline on the document page


KINDS = {
    kind.name: kind
    for kind in (
        Kind("document", "Tài liệu", "file", 50, extractable=True),
        Kind("presentation", "Trình chiếu", "presentation", 100, extractable=True),
        Kind("data", "Dữ liệu", "table", 50, extractable=True),
        Kind("image", "Hình ảnh", "image", 25, preview="image"),
        Kind("audio", "Âm thanh", "music", 100, preview="audio"),
        Kind("video", "Video", "video", 500, preview="video"),
        Kind("archive", "Tệp nén", "archive", 200),
    )
}

# extension -> (MIME type served back, kind)
EXTENSIONS = {
    ".txt": ("text/plain", "document"),
    ".md": ("text/markdown", "document"),
    ".pdf": ("application/pdf", "document"),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "document"),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "presentation"),
    ".csv": ("text/csv", "data"),
    ".json": ("application/json", "data"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "data"),
    ".png": ("image/png", "image"),
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".gif": ("image/gif", "image"),
    ".webp": ("image/webp", "image"),
    ".mp3": ("audio/mpeg", "audio"),
    ".wav": ("audio/wav", "audio"),
    ".m4a": ("audio/mp4", "audio"),
    ".ogg": ("audio/ogg", "audio"),
    ".mp4": ("video/mp4", "video"),
    ".webm": ("video/webm", "video"),
    ".mov": ("video/quicktime", "video"),
    ".zip": ("application/zip", "archive"),
}


def suffix_of(name):
    dot = (name or "").rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def kind_of(name):
    """Kind for a file name (or stored object name); None if not allowed."""
    entry = EXTENSIONS.get(suffix_of(name))
    return KINDS[entry[1]] if entry else None


def mime_of(name):
    return EXTENSIONS[suffix_of(name)][0]


def limit_bytes(kind):
    raw = os.environ.get(f"MAX_UPLOAD_MB_{kind.name.upper()}", "").strip()
    megabytes = int(raw) if raw.isdigit() and int(raw) > 0 else kind.default_limit_mb
    return megabytes * 1024 * 1024


def accept_attribute():
    """For <input type=file accept=...>."""
    return ",".join(EXTENSIONS)


def limits_by_extension():
    """{".mp4": bytes, ...} for the browser-side size check."""
    return {ext: limit_bytes(KINDS[kind]) for ext, (_, kind) in EXTENSIONS.items()}


def summary():
    """Groups for the upload hint, e.g. [("Video", ".mp4 .webm .mov", 500)]."""
    groups = []
    for kind in KINDS.values():
        exts = [ext for ext, (_, k) in EXTENSIONS.items() if k == kind.name]
        groups.append((kind.label, " ".join(exts), limit_bytes(kind) // (1024 * 1024)))
    return groups
