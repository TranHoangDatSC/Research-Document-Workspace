"""Allowed file types, their kinds and size limits: one source for the upload
check, the browser file picker, previews and text extraction.

Only the extension is checked. Files are served back with the allowlisted
MIME type and `nosniff`, so a renamed file can't run as something else.

Per-kind limits (MiB) can be overridden with MAX_UPLOAD_MB_<KIND>. Uploads
stream to MinIO, so a higher limit costs disk, not RAM.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Kind:
    name: str
    label: str
    icon: str
    default_limit_mb: int
    # "local" = parsed here (extractors.py), "ai" = read by Gemini (media_ai.py).
    text_via: str = "local"
    preview: str | None = None  # "image" | "audio" | "video": shown inline on the document page

    @property
    def needs_ai(self):
        return self.text_via == "ai"


KINDS = {
    kind.name: kind
    for kind in (
        Kind("document", "Tài liệu", "file", 50),
        Kind("presentation", "Trình chiếu", "presentation", 100),
        Kind("data", "Dữ liệu", "table", 50),
        Kind("image", "Hình ảnh", "image", 25, text_via="ai", preview="image"),
        Kind("audio", "Âm thanh", "music", 100, text_via="ai", preview="audio"),
        Kind("video", "Video", "video", 500, text_via="ai", preview="video"),
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
