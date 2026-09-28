"""Run with: python -m unittest discover -s tests/unit -v
Ask-your-documents (simplified RAG): chunking/ranking are pure functions;
the LLM boundary and the service orchestration are tested with fakes —
no real network call, no API key needed.
"""
import io
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
from uuid import uuid4

os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-production")

from fastapi import HTTPException

from app import llm, rag
from app.repositories import documents as repository
from app.services import documents as documents_service
from app.services import rag as service


class ChunkTextTests(unittest.TestCase):
    def test_empty_text(self):
        self.assertEqual(rag.chunk_text(""), [])
        self.assertEqual(rag.chunk_text("   "), [])

    def test_short_text_single_chunk(self):
        self.assertEqual(rag.chunk_text("hello world", chunk_characters=1000), ["hello world"])

    def test_long_text_splits_with_overlap(self):
        text = "0123456789" * 30  # 300 chars
        chunks = rag.chunk_text(text, chunk_characters=100, overlap=20)
        self.assertGreater(len(chunks), 1)
        # Every character of the source must appear in some chunk (overlap, not gaps).
        self.assertTrue(all(c in "".join(chunks) for c in set(text)))
        self.assertEqual(chunks[0], text[:100])
        self.assertEqual(chunks[1], text[80:180])

    def test_last_chunk_not_padded_past_text_end(self):
        chunks = rag.chunk_text("a" * 150, chunk_characters=100, overlap=20)
        self.assertLessEqual(len(chunks[-1]), 100)
        self.assertEqual(chunks[-1], "a" * len(chunks[-1]))


class BuildChunksTests(unittest.TestCase):
    def test_skips_documents_without_extraction(self):
        docs = [
            {"document_id": "1", "original_name": "a.txt", "extracted_text": None},
            {"document_id": "2", "original_name": "b.txt"},  # missing key entirely
            {"document_id": "3", "original_name": "c.txt", "extracted_text": {"text": ""}},
        ]
        self.assertEqual(rag.build_chunks(docs), [])

    def test_indexes_chunks_per_document(self):
        docs = [
            {"document_id": "1", "original_name": "a.txt", "extracted_text": {"text": "hello world"}},
            {"document_id": "2", "original_name": "b.txt", "extracted_text": {"text": "second document"}},
        ]
        chunks = rag.build_chunks(docs)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], {"document_id": "1", "original_name": "a.txt", "chunk_index": 0, "text": "hello world"})
        self.assertEqual(chunks[1]["document_id"], "2")
        self.assertEqual(chunks[1]["chunk_index"], 0)


class RankChunksTests(unittest.TestCase):
    def test_no_question_words_returns_empty(self):
        chunks = [{"document_id": "1", "original_name": "a", "chunk_index": 0, "text": "some text"}]
        self.assertEqual(rag.rank_chunks("   ", chunks), [])

    def test_ranks_by_keyword_overlap(self):
        chunks = [
            {"document_id": "1", "original_name": "a", "chunk_index": 0, "text": "cloud docker kubernetes"},
            {"document_id": "2", "original_name": "b", "chunk_index": 0, "text": "cloud docker compose backup restore"},
            {"document_id": "3", "original_name": "c", "chunk_index": 0, "text": "completely unrelated recipe for soup"},
        ]
        ranked = rag.rank_chunks("cloud docker backup", chunks)
        self.assertEqual([c["document_id"] for c in ranked], ["2", "1"])

    def test_respects_top_k(self):
        chunks = [
            {"document_id": str(i), "original_name": "x", "chunk_index": 0, "text": "keyword " * (i + 1)}
            for i in range(10)
        ]
        ranked = rag.rank_chunks("keyword", chunks, top_k=3)
        self.assertEqual(len(ranked), 3)

    def test_zero_overlap_chunks_excluded(self):
        chunks = [{"document_id": "1", "original_name": "a", "chunk_index": 0, "text": "nothing matches here"}]
        self.assertEqual(rag.rank_chunks("docker", chunks), [])


class BuildPromptTests(unittest.TestCase):
    def test_includes_document_name_and_chunk_number(self):
        chunks = [{"document_id": "1", "original_name": "paper.pdf", "chunk_index": 2, "text": "some content"}]
        prompt = rag.build_prompt("what is X?", chunks)
        self.assertIn("paper.pdf", prompt)
        self.assertIn("đoạn 3", prompt)  # chunk_index 2 -> displayed as 1-based
        self.assertIn("some content", prompt)
        self.assertIn("what is X?", prompt)

    def test_empty_chunks_uses_placeholder(self):
        prompt = rag.build_prompt("anything", [])
        self.assertIn("Không tìm thấy đoạn văn bản liên quan", prompt)


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_API_KEYS", "LLM_MODELS"):
            os.environ.pop(key, None)

    def test_missing_config_raises(self):
        with self.assertRaises(llm.LLMError):
            llm.ask("hello")

    def test_unsupported_provider_raises(self):
        os.environ["LLM_PROVIDER"] = "carrier-pigeon"
        os.environ["LLM_API_KEY"] = "x"
        with self.assertRaises(llm.LLMError):
            llm.ask("hello")

    def test_gemini_response_parsed(self):
        os.environ["LLM_PROVIDER"] = "gemini"
        os.environ["LLM_API_KEY"] = "fake-key"
        fake_response = MagicMock()
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda self, *a: None
        fake_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "Gemini answer"}]}}]}'
        )
        with patch("urllib.request.urlopen", return_value=fake_response):
            answer, model = llm.ask("hello")
        self.assertEqual(answer, "Gemini answer")
        self.assertTrue(model)

    def test_openai_response_parsed(self):
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["LLM_API_KEY"] = "fake-key"
        fake_response = MagicMock()
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda self, *a: None
        fake_response.read.return_value = (
            b'{"choices": [{"message": {"content": "OpenAI answer"}}]}'
        )
        with patch("urllib.request.urlopen", return_value=fake_response):
            answer, model = llm.ask("hello")
        self.assertEqual(answer, "OpenAI answer")
        self.assertTrue(model)

    def test_malformed_gemini_response_raises_llmerror(self):
        os.environ["LLM_PROVIDER"] = "gemini"
        os.environ["LLM_API_KEY"] = "fake-key"
        fake_response = MagicMock()
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda self, *a: None
        fake_response.read.return_value = b'{"unexpected": "shape"}'
        with patch("urllib.request.urlopen", return_value=fake_response):
            with self.assertRaises(llm.LLMError):
                llm.ask("hello")

    def test_rotates_to_next_model_when_one_is_overloaded(self):
        os.environ["LLM_PROVIDER"] = "gemini"
        os.environ["LLM_API_KEY"] = "fake-key"
        os.environ["LLM_MODELS"] = "model-a,model-b"

        good_response = MagicMock()
        good_response.__enter__ = lambda self: self
        good_response.__exit__ = lambda self, *a: None
        good_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "answer from b"}]}}]}'
        )

        def fake_urlopen(req, timeout=None):
            if "model-a" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 503, "overloaded", {}, io.BytesIO(b"overloaded"))
            return good_response

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            answer, model = llm.ask("hello")
        self.assertEqual(answer, "answer from b")
        self.assertEqual(model, "model-b")

    def test_preferred_model_tried_first(self):
        os.environ["LLM_PROVIDER"] = "gemini"
        os.environ["LLM_API_KEY"] = "fake-key"
        os.environ["LLM_MODELS"] = "model-a,model-b,model-c"

        calls = []
        good_response = MagicMock()
        good_response.__enter__ = lambda self: self
        good_response.__exit__ = lambda self, *a: None
        good_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}'
        )

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return good_response

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            answer, model = llm.ask("hello", preferred_model="model-c")
        self.assertEqual(model, "model-c")
        self.assertIn("model-c", calls[0])

    def test_rotates_to_next_key_after_all_models_fail(self):
        os.environ["LLM_PROVIDER"] = "gemini"
        os.environ["LLM_API_KEYS"] = "bad-key,good-key"
        os.environ["LLM_MODELS"] = "model-a"

        good_response = MagicMock()
        good_response.__enter__ = lambda self: self
        good_response.__exit__ = lambda self, *a: None
        good_response.read.return_value = (
            b'{"candidates": [{"content": {"parts": [{"text": "ok with good key"}]}}]}'
        )

        def fake_urlopen(req, timeout=None):
            if "key=bad-key" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 429, "quota", {}, io.BytesIO(b"quota exceeded"))
            return good_response

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            answer, model = llm.ask("hello")
        self.assertEqual(answer, "ok with good key")

    def test_all_combinations_failing_reports_attempt_count(self):
        os.environ["LLM_PROVIDER"] = "gemini"
        os.environ["LLM_API_KEYS"] = "key1,key2"
        os.environ["LLM_MODELS"] = "model-a,model-b"

        def always_fails(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 503, "overloaded", {}, io.BytesIO(b"x"))

        with patch("urllib.request.urlopen", side_effect=always_fails):
            with self.assertRaises(llm.LLMError) as caught:
                llm.ask("hello")
        self.assertIn("4", caught.exception.message)  # 2 keys x 2 models


class AskProjectServiceTests(unittest.TestCase):
    def setUp(self):
        self.project_id = uuid4()
        self.rows = []
        self.details = {}
        patches = [
            patch.object(documents_service, "require_project", lambda pid: None),
            patch.object(repository, "list_documents", lambda pid, limit, offset: list(self.rows)),
            patch.object(repository, "get_details", lambda did: self.details.get(did)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def add_document(self, name, text, status="ready"):
        doc_id = uuid4()
        self.rows.append({"id": doc_id, "original_name": name, "status": status})
        self.details[doc_id] = {"document_id": str(doc_id), "extracted_text": {"text": text} if text else None}
        return doc_id

    def test_empty_question_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            service.ask_project(self.project_id, "   ")
        self.assertEqual(caught.exception.status_code, 422)

    def test_no_extracted_documents_returns_409(self):
        self.add_document("a.txt", None)
        with self.assertRaises(HTTPException) as caught:
            service.ask_project(self.project_id, "what is in here?")
        self.assertEqual(caught.exception.status_code, 409)

    def test_non_ready_documents_ignored(self):
        self.add_document("a.txt", "cloud docker content", status="pending")
        with self.assertRaises(HTTPException) as caught:
            service.ask_project(self.project_id, "docker")
        self.assertEqual(caught.exception.status_code, 409)

    def test_success_returns_answer_sources_and_model(self):
        self.add_document("cloud.txt", "This document explains Docker and Cloud deployment.")
        with patch.object(llm, "ask", lambda prompt, preferred_model=None: ("Canned answer", "fake-model")):
            result = service.ask_project(self.project_id, "What does it say about Docker?")
        self.assertEqual(result["answer"], "Canned answer")
        self.assertEqual(result["model"], "fake-model")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["original_name"], "cloud.txt")

    def test_model_choice_passed_through_to_llm(self):
        self.add_document("cloud.txt", "Docker content.")
        received = {}
        def fake_ask(prompt, preferred_model=None):
            received["model"] = preferred_model
            return "answer", preferred_model
        with patch.object(llm, "ask", fake_ask):
            service.ask_project(self.project_id, "docker?", model="gemini-1.5-flash")
        self.assertEqual(received["model"], "gemini-1.5-flash")

    def test_llm_failure_returns_503(self):
        self.add_document("cloud.txt", "Docker and cloud content here.")
        def failing_ask(prompt, preferred_model=None):
            raise llm.LLMError("simulated failure")
        with patch.object(llm, "ask", failing_ask):
            with self.assertRaises(HTTPException) as caught:
                service.ask_project(self.project_id, "docker?")
        self.assertEqual(caught.exception.status_code, 503)

    def test_document_ids_scopes_answer_to_selected_sources(self):
        kept = self.add_document("cloud.txt", "Docker and cloud content here.")
        self.add_document("recipe.txt", "Docker and cloud content here.")
        with patch.object(llm, "ask", lambda prompt, preferred_model=None: ("ok", "fake-model")):
            result = service.ask_project(self.project_id, "docker?", document_ids=[str(kept)])
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["document_id"], str(kept))

    def test_empty_document_ids_rejected(self):
        self.add_document("cloud.txt", "Docker content.")
        with self.assertRaises(HTTPException) as caught:
            service.ask_project(self.project_id, "docker?", document_ids=[])
        self.assertEqual(caught.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
