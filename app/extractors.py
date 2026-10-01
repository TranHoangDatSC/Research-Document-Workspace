"""Pure text extraction: no database, storage, network, or logging."""

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader


MAX_EXTRACTED_CHARACTERS = 200_000
# Extraction reads the whole file into memory (the parsers need it), so this
# stays well under the web container's 512 MB even though uploads may be
# larger: a bigger file is still stored, it just can't be text-extracted.
MAX_INPUT_BYTES = 50 * 1024 * 1024


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


def _shape_texts(shape):
    if shape.has_text_frame:
        yield shape.text_frame.text
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            yield "\t".join(cell.text for cell in row.cells)
    if shape.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
        for inner in shape.shapes:
            yield from _shape_texts(inner)


def _extract_pptx(content: bytes) -> tuple[str, str]:
    """Slide by slide: text boxes, tables and speaker notes, with a slide heading
    so an answer can say which slide something came from."""
    try:
        presentation = Presentation(io.BytesIO(content))
        slides = []
        for number, slide in enumerate(presentation.slides, start=1):
            parts = [t for shape in slide.shapes for t in _shape_texts(shape) if t.strip()]
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text
                if notes.strip():
                    parts.append("Ghi chú: " + notes)
            if parts:
                slides.append(f"--- Slide {number} ---\n" + "\n".join(parts))
        text = "\n\n".join(slides)
    except Exception:
        raise ExtractionError("corrupt_file") from None
    return text, "pptx_text"


def _extract_xlsx(content: bytes) -> tuple[str, str]:
    """Sheet by sheet, one tab-separated line per non-empty row, cached cell
    values (not formulas). read_only streams rows instead of building every
    cell object, which matters for big sheets."""
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            sheets = []
            for sheet in workbook.worksheets:
                lines = []
                for row in sheet.iter_rows(values_only=True):
                    cells = ["" if value is None else str(value) for value in row]
                    while cells and cells[-1] == "":
                        cells.pop()
                    if cells:
                        lines.append("\t".join(cells))
                if lines:
                    sheets.append(f"--- Sheet: {sheet.title} ---\n" + "\n".join(lines))
        finally:
            workbook.close()
        text = "\n\n".join(sheets)
    except Exception:
        raise ExtractionError("corrupt_file") from None
    return text, "xlsx_text"


ZIP_MAX_MEMBERS_LISTED = 500
ZIP_MAX_UNCOMPRESSED = 50 * 1024 * 1024  # zip-bomb guard: total bytes ever decompressed
ZIP_MAX_MEMBER = 20 * 1024 * 1024


def _extract_zip(content: bytes) -> tuple[str, str]:
    """A listing of everything inside, then the text of each member this
    module can read (documents, slides, sheets, data) — nested zips, media
    and encrypted members are listed but not opened. Decompression is
    capped (per member and in total), so a zip bomb just stops early."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        members = [m for m in archive.infolist() if not m.is_dir()]
    except (zipfile.BadZipFile, OSError, ValueError):
        raise ExtractionError("corrupt_file") from None

    listing = [f"- {m.filename} ({m.file_size} bytes)" for m in members[:ZIP_MAX_MEMBERS_LISTED]]
    if len(members) > ZIP_MAX_MEMBERS_LISTED:
        listing.append(f"- … và {len(members) - ZIP_MAX_MEMBERS_LISTED} tệp khác")
    sections = [f"--- Nội dung tệp nén ({len(members)} tệp) ---\n" + "\n".join(listing)]

    budget = ZIP_MAX_UNCOMPRESSED
    for member in members:
        suffix = PurePosixPath(member.filename).suffix.lower()
        if suffix not in _EXTRACTORS or suffix == ".zip" or member.flag_bits & 0x1:  # 0x1 = encrypted
            continue
        if member.file_size > min(ZIP_MAX_MEMBER, budget):
            sections.append(f"--- {member.filename} ---\n(bỏ qua: quá lớn để đọc trong tệp nén)")
            continue
        try:
            with archive.open(member) as handle:
                data = handle.read(member.file_size + 1)  # never trust the declared size beyond +1
            if len(data) > member.file_size:
                raise ExtractionError("corrupt_file")
            budget -= len(data)
            text, _ = _EXTRACTORS[suffix](data)
        except (ExtractionError, zipfile.BadZipFile, OSError, RuntimeError, ValueError):
            sections.append(f"--- {member.filename} ---\n(không đọc được)")
            continue
        if text.strip():
            sections.append(f"--- {member.filename} ---\n{text.strip()}")
    return "\n\n".join(sections), "zip_text"


_EXTRACTORS = {
    ".txt": _extract_txt,
    ".md": _extract_txt,
    ".csv": _extract_txt,
    ".json": _extract_txt,
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".pptx": _extract_pptx,
    ".xlsx": _extract_xlsx,
}
_EXTRACTORS[".zip"] = _extract_zip  # after the dict: _extract_zip looks members up in it


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
    """Extract text from .txt/.md/.csv/.json/.pdf/.docx/.pptx/.xlsx bytes. Counts describe the saved text."""
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


def from_text(text: str, method: str) -> ExtractionResult:
    """Same normalising, counting and truncation as extract_text, for text
    produced elsewhere (e.g. Gemini reading an image or a video)."""
    return _finalize(text, method, MAX_EXTRACTED_CHARACTERS)
