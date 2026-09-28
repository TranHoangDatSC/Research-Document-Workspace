"""Pure text extraction: no database, storage, network, or logging."""

import io
from dataclasses import dataclass

from docx import Document
from pypdf import PdfReader


MAX_EXTRACTED_CHARACTERS = 200_000
MAX_INPUT_BYTES = 10 * 1024 * 1024


class ExtractionError(Exception):
    """A controlled extraction failure with a public error code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    method: str
    character_count: int
    word_count: int
    truncated: bool


def _extract_txt(content: bytes) -> tuple[str, str]:
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise ExtractionError("invalid_utf8") from None
    return text, "plain_text"


def _extract_pdf(content: bytes) -> tuple[str, str]:
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ExtractionError("encrypted_file")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError("corrupt_file") from None
    return text, "pdf_text"


def _extract_docx(content: bytes) -> tuple[str, str]:
    try:
        document = Document(io.BytesIO(content))
        parts = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.extend(cell.text for cell in row.cells)
        text = "\n".join(parts)
    except Exception:
        raise ExtractionError("corrupt_file") from None
    return text, "docx_text"


_EXTRACTORS = {
    ".txt": _extract_txt,
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
}


def _finalize(text: str, method: str, max_characters: int) -> ExtractionResult:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in normalized:
        raise ExtractionError("invalid_text")
    if not normalized.strip():
        normalized = ""

    saved_text = normalized[:max_characters]

    return ExtractionResult(
        text=saved_text,
        method=method,
        character_count=len(saved_text),
        word_count=len(saved_text.split()),
        truncated=len(normalized) > max_characters,
    )


def extract_text(
    content: bytes,
    extension: str,
    max_characters: int = MAX_EXTRACTED_CHARACTERS,
) -> ExtractionResult:
    """Extract text from .txt/.pdf/.docx bytes. Counts describe the saved text."""
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")

    if (
        isinstance(max_characters, bool)
        or not isinstance(max_characters, int)
        or not 1 <= max_characters <= MAX_EXTRACTED_CHARACTERS
    ):
        raise ValueError(
            "max_characters must be an integer between 1 and 200000"
        )

    suffix = extension.strip().lower()
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        raise ExtractionError("unsupported_format")

    if len(content) > MAX_INPUT_BYTES:
        raise ExtractionError("input_too_large")

    text, method = extractor(content)
    return _finalize(text, method, max_characters)
