import io
import unittest
from unittest.mock import patch
from uuid import uuid4

from docx import Document
from fastapi import HTTPException
from pypdf import PdfWriter

from app.extractors import (
    MAX_EXTRACTED_CHARACTERS,
    MAX_INPUT_BYTES,
    ExtractionError,
    extract_text,
)
from app.repositories import documents as repository
from app.services import documents as service


def build_pdf(text="Hello PDF extraction"):
    """Hand-built single-page PDF (no external renderer) with a real text stream."""
    content = f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 200 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode("latin-1")
        + content
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode("latin-1")
    return bytes(out)


def build_blank_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def build_encrypted_pdf(password="secret"):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def build_docx(paragraphs=("Hello DOCX extraction",)):
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class TextExtractionTests(unittest.TestCase):
    def assert_extraction_error(self, code, content, extension):
        with self.assertRaises(ExtractionError) as caught:
            extract_text(content, extension)
        self.assertEqual(caught.exception.code, code)

    def test_utf8_vietnamese(self):
        text = "T\u00e0i li\u1ec7u nghi\u00ean c\u1ee9u\nCloud Docker"
        result = extract_text(text.encode("utf-8"), ".txt")

        self.assertEqual(result.text, text)
        self.assertEqual(result.method, "plain_text")
        self.assertEqual(result.character_count, len(text))
        self.assertEqual(result.word_count, 6)
        self.assertFalse(result.truncated)

    def test_bom_and_line_endings(self):
        content = b"\xef\xbb\xbfFirst\r\nSecond\rThird"
        result = extract_text(content, " .TXT ")

        self.assertEqual(result.text, "First\nSecond\nThird")
        self.assertEqual(result.word_count, 3)

    def test_empty_and_whitespace(self):
        for content in (b"", b" \t\r\n", b"\xef\xbb\xbf"):
            with self.subTest(content=content):
                result = extract_text(content, ".txt")
                self.assertEqual(result.text, "")
                self.assertEqual(result.character_count, 0)
                self.assertEqual(result.word_count, 0)
                self.assertFalse(result.truncated)

    def test_invalid_utf8(self):
        self.assert_extraction_error(
            "invalid_utf8", b"\xff\xfe", ".txt"
        )

    def test_nul_character(self):
        self.assert_extraction_error(
            "invalid_text", b"hello\x00world", ".txt"
        )

    def test_truncation_counts_saved_text(self):
        result = extract_text(
            b"one two three", ".txt", max_characters=7
        )

        self.assertEqual(result.text, "one two")
        self.assertEqual(result.character_count, 7)
        self.assertEqual(result.word_count, 2)
        self.assertTrue(result.truncated)

    def test_exact_limit_is_not_truncated(self):
        result = extract_text(b"abcde", ".txt", max_characters=5)

        self.assertEqual(result.text, "abcde")
        self.assertFalse(result.truncated)

    def test_default_limit(self):
        content = b"a" * (MAX_EXTRACTED_CHARACTERS + 1)
        result = extract_text(content, ".txt")

        self.assertEqual(
            result.character_count, MAX_EXTRACTED_CHARACTERS
        )
        self.assertTrue(result.truncated)

    def test_input_size_limit(self):
        self.assert_extraction_error(
            "input_too_large",
            b"a" * (MAX_INPUT_BYTES + 1),
            ".txt",
        )

    def test_unsupported_format(self):
        self.assert_extraction_error(
            "unsupported_format", b"hello", ".exe"
        )

    def test_invalid_character_limit(self):
        for limit in (0, -1, True, 1.5, 200_001):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    extract_text(b"hello", ".txt", limit)

    def test_content_must_be_bytes(self):
        with self.assertRaises(TypeError):
            extract_text("hello", ".txt")


class PdfExtractionTests(unittest.TestCase):
    def test_pdf_text(self):
        result = extract_text(build_pdf("Hello PDF extraction"), ".pdf")

        self.assertEqual(result.text, "Hello PDF extraction")
        self.assertEqual(result.method, "pdf_text")
        self.assertFalse(result.truncated)

    def test_pdf_uppercase_extension(self):
        result = extract_text(build_pdf("case"), " .PDF ")
        self.assertEqual(result.text, "case")

    def test_pdf_blank_page_is_empty_not_error(self):
        result = extract_text(build_blank_pdf(), ".pdf")

        self.assertEqual(result.text, "")
        self.assertEqual(result.character_count, 0)
        self.assertEqual(result.method, "pdf_text")

    def test_pdf_encrypted(self):
        with self.assertRaises(ExtractionError) as caught:
            extract_text(build_encrypted_pdf(), ".pdf")
        self.assertEqual(caught.exception.code, "encrypted_file")

    def test_pdf_corrupt(self):
        with self.assertRaises(ExtractionError) as caught:
            extract_text(b"not a pdf file", ".pdf")
        self.assertEqual(caught.exception.code, "corrupt_file")

    def test_pdf_truncation(self):
        result = extract_text(build_pdf("one two three"), ".pdf", max_characters=7)

        self.assertEqual(result.text, "one two")
        self.assertTrue(result.truncated)

    def test_pdf_too_large(self):
        with self.assertRaises(ExtractionError) as caught:
            extract_text(b"a" * (MAX_INPUT_BYTES + 1), ".pdf")
        self.assertEqual(caught.exception.code, "input_too_large")


class DocxExtractionTests(unittest.TestCase):
    def test_docx_text(self):
        result = extract_text(
            build_docx(["First paragraph", "Second paragraph"]), ".docx"
        )

        self.assertEqual(result.text, "First paragraph\nSecond paragraph")
        self.assertEqual(result.method, "docx_text")
        self.assertFalse(result.truncated)

    def test_docx_uppercase_extension(self):
        result = extract_text(build_docx(["case"]), " .DOCX ")
        self.assertEqual(result.text, "case")

    def test_docx_no_paragraphs_is_empty_not_error(self):
        result = extract_text(build_docx([]), ".docx")

        self.assertEqual(result.text, "")
        self.assertEqual(result.character_count, 0)
        self.assertEqual(result.method, "docx_text")

    def test_docx_corrupt(self):
        with self.assertRaises(ExtractionError) as caught:
            extract_text(b"not a docx file", ".docx")
        self.assertEqual(caught.exception.code, "corrupt_file")

    def test_docx_wrong_zip_content_is_corrupt(self):
        # A well-formed ZIP that is not a Word package must still be rejected cleanly.
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "not a docx package")
        with self.assertRaises(ExtractionError) as caught:
            extract_text(buffer.getvalue(), ".docx")
        self.assertEqual(caught.exception.code, "corrupt_file")

    def test_docx_truncation(self):
        result = extract_text(
            build_docx(["one two three"]), ".docx", max_characters=7
        )

        self.assertEqual(result.text, "one two")
        self.assertTrue(result.truncated)

    def test_docx_too_large(self):
        with self.assertRaises(ExtractionError) as caught:
            extract_text(b"a" * (MAX_INPUT_BYTES + 1), ".docx")
        self.assertEqual(caught.exception.code, "input_too_large")


class ObjectResponse(io.BytesIO):
    def release_conn(self):
        pass


class ExtractServiceTests(unittest.TestCase):
    """extract_document(): real logic, fakes only the MinIO/MongoDB boundaries."""

    def setUp(self):
        self.document_id = uuid4()
        self.payload = b""
        self.saved = {}
        self.fail_download = False

        self.row = {
            "id": self.document_id,
            "project_id": uuid4(),
            "original_name": "paper.txt",
            "object_name": f"documents/{self.document_id}/original.txt",
            "content_type": "text/plain",
            "size_bytes": 0,
            "status": "ready",
        }

        class FakeMinio:
            def __init__(outer_self, outer):
                outer_self.outer = outer

            def get_object(outer_self, bucket, key):
                if outer_self.outer.fail_download:
                    raise RuntimeError("simulated minio failure")
                return ObjectResponse(outer_self.outer.payload)

        def update_extracted_text(document_id, extracted):
            self.saved[document_id] = extracted

        def get_details(document_id):
            return {"document_id": str(document_id), "extracted_text": self.saved.get(document_id)}

        patches = [
            patch.object(service, "document_row", lambda did: dict(self.row)),
            patch.object(service, "minio_client", lambda: FakeMinio(self)),
            patch.object(service, "bucket_name", lambda: "test"),
            patch.object(repository, "update_extracted_text", update_extracted_text),
            patch.object(repository, "get_details", get_details),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def set_file(self, suffix, payload):
        self.row["object_name"] = f"documents/{self.document_id}/original{suffix}"
        self.row["size_bytes"] = len(payload)
        self.payload = payload

    def test_txt_extraction_persists_to_mongodb(self):
        self.set_file(".txt", "Xin chào Cloud Docker".encode("utf-8"))

        result = service.extract_document(self.document_id)

        self.assertEqual(result["extracted_text"]["text"], "Xin chào Cloud Docker")
        self.assertEqual(result["extracted_text"]["method"], "plain_text")
        self.assertFalse(result["extracted_text"]["truncated"])
        self.assertIn("extracted_at", result["extracted_text"])
        # What get_document() returns must match exactly what was persisted.
        self.assertEqual(self.saved[self.document_id], result["extracted_text"])

    def test_pdf_and_docx_persist_too(self):
        self.set_file(".pdf", build_pdf("From a PDF"))
        result = service.extract_document(self.document_id)
        self.assertEqual(result["extracted_text"]["text"], "From a PDF")
        self.assertEqual(result["extracted_text"]["method"], "pdf_text")

        self.set_file(".docx", build_docx(["From a DOCX"]))
        result = service.extract_document(self.document_id)
        self.assertEqual(result["extracted_text"]["text"], "From a DOCX")
        self.assertEqual(result["extracted_text"]["method"], "docx_text")

    def test_reextraction_overwrites_previous_result(self):
        self.set_file(".txt", b"first version")
        service.extract_document(self.document_id)
        self.set_file(".txt", b"second version")
        result = service.extract_document(self.document_id)

        self.assertEqual(result["extracted_text"]["text"], "second version")
        self.assertEqual(self.saved[self.document_id]["text"], "second version")

    def test_corrupt_file_returns_422_and_does_not_persist(self):
        self.set_file(".pdf", b"not a pdf")

        with self.assertRaises(HTTPException) as caught:
            service.extract_document(self.document_id)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertNotIn(self.document_id, self.saved)

    def test_encrypted_pdf_returns_422_and_does_not_persist(self):
        self.set_file(".pdf", build_encrypted_pdf())

        with self.assertRaises(HTTPException) as caught:
            service.extract_document(self.document_id)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertNotIn(self.document_id, self.saved)

    def test_not_ready_document_returns_409(self):
        self.row["status"] = "pending"
        self.set_file(".txt", b"hello")

        with self.assertRaises(HTTPException) as caught:
            service.extract_document(self.document_id)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertNotIn(self.document_id, self.saved)

    def test_minio_download_failure_returns_503(self):
        self.set_file(".txt", b"hello")
        self.fail_download = True

        with self.assertRaises(HTTPException) as caught:
            service.extract_document(self.document_id)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn(self.document_id, self.saved)

    def test_mongodb_persist_failure_returns_503(self):
        self.set_file(".txt", b"hello")

        def failing_update(document_id, extracted):
            raise RuntimeError("simulated mongo failure")

        with patch.object(repository, "update_extracted_text", failing_update):
            with self.assertRaises(HTTPException) as caught:
                service.extract_document(self.document_id)
        self.assertEqual(caught.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
