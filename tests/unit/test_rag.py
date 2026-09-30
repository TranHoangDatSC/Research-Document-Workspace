"""Run with: python -m unittest discover -s tests/unit -v
Ask-your-documents (simplified RAG): chunking/ranking are pure functions;
the LLM boundary and the service orchestration are tested with fakes —
no real network call, no API key needed.
"""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
from uuid import uuid4

os.environ.setdefault("SESSION_SECRET", "test-secret-not-for-production")

from fastapi import HTTPException

from app import domains, llm, rag
from app.repositories import documents as repository
from app.services import documents as documents_service
from app.services import rag as service


class ChunkTextTests(unittest.TestCase):
    def test_empty_text(self):
        self.assertEqual(rag.chunk_text(""), [])
        self.assertEqual(rag.chunk_text("   "), [])

    def test_short_text_single_chunk(self):
        self.assertEqual(rag.chunk_text("hello world", chunk_characters=1000), ["hello world"])

    def test_packs_whole_paragraphs(self):
        paragraphs = [f"Paragraph {i} " + "x" * 30 for i in range(6)]  # ~42 chars each
        chunks = rag.chunk_text("\n\n".join(paragraphs), chunk_characters=100, overlap=0)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)
            # No paragraph is cut in half.
            for line in chunk.split("\n"):
                self.assertIn(line, paragraphs)
        self.assertEqual(sum(len(c.split("\n")) for c in chunks), 6)

    def test_overlap_repeats_trailing_paragraph(self):
        paragraphs = [f"P{i} " + "y" * 20 for i in range(8)]  # 23 chars each
        chunks = rag.chunk_text("\n\n".join(paragraphs), chunk_characters=60, overlap=30)
        # The last paragraph of one chunk opens the next.
        self.assertEqual(chunks[0].split("\n")[-1], chunks[1].split("\n")[0])

    def test_long_paragraph_splits_on_sentences(self):
        text = "First sentence here. Second sentence here. Third sentence here."
        chunks = rag.chunk_text(text, chunk_characters=45, overlap=0)
        self.assertEqual(chunks, ["First sentence here.\nSecond sentence here.", "Third sentence here."])

    def test_unbreakable_text_is_hard_cut(self):
        chunks = rag.chunk_text("a" * 250, chunk_characters=100, overlap=20)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])


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

    def test_unaccented_question_matches_vietnamese_text(self):
        chunks = [
            {"document_id": "1", "original_name": "a", "chunk_index": 0, "text": "Học sinh tiểu học và động lực học tập"},
            {"document_id": "2", "original_name": "b", "chunk_index": 0, "text": "Kinh tế vĩ mô và lạm phát"},
        ]
        ranked = rag.rank_chunks("dong luc hoc tap cua hoc sinh", chunks)
        self.assertEqual([c["document_id"] for c in ranked], ["1"])

    def test_stopwords_alone_do_not_match(self):
        chunks = [{"document_id": "1", "original_name": "a", "chunk_index": 0, "text": "của các và là những một"}]
        self.assertEqual(rag.rank_chunks("là gì của các", chunks), [])

    def test_bigram_prefers_the_exact_phrase(self):
        chunks = [
            {"document_id": "1", "original_name": "a", "chunk_index": 0, "text": "chính sách tiền tệ của ngân hàng"},
            {"document_id": "2", "original_name": "b", "chunk_index": 0, "text": "tiền lương theo chính phủ, sách tệ"},
        ]
        ranked = rag.rank_chunks("chính sách tiền tệ", chunks)
        self.assertEqual(ranked[0]["document_id"], "1")

    def test_weak_matches_below_relative_floor_dropped(self):
        strong = "docker compose backup restore volume " * 3
        chunks = [
            {"document_id": "1", "original_name": "a", "chunk_index": 0, "text": strong},
            {"document_id": "2", "original_name": "b", "chunk_index": 0, "text": "one mention of volume among many other unrelated words here"},
        ] + [
            {"document_id": str(i), "original_name": "x", "chunk_index": 0, "text": "unrelated filler text"}
            for i in range(3, 10)
        ]
        ranked = rag.rank_chunks("docker compose backup restore volume", chunks, min_relative_score=0.5)
        self.assertEqual([c["document_id"] for c in ranked], ["1"])


class OrderForReadingTests(unittest.TestCase):
    def test_groups_by_document_then_chunk_order(self):
        chunks = [
            {"document_id": "b", "chunk_index": 3},
            {"document_id": "a", "chunk_index": 5},
            {"document_id": "b", "chunk_index": 1},
            {"document_id": "a", "chunk_index": 0},
        ]
        ordered = rag.order_for_reading(chunks)
        self.assertEqual([(c["document_id"], c["chunk_index"]) for c in ordered], [("b", 1), ("b", 3), ("a", 0), ("a", 5)])


class BuildPromptTests(unittest.TestCase):
    def test_numbers_passages_with_document_name_and_chunk(self):
        chunks = [
            {"document_id": "1", "original_name": "paper.pdf", "chunk_index": 2, "text": "some content"},
            {"document_id": "2", "original_name": "other.pdf", "chunk_index": 0, "text": "more content"},
        ]
        prompt = rag.build_prompt("what is X?", chunks)
        self.assertIn("[1] paper.pdf — đoạn 3", prompt)  # chunk_index 2 -> displayed as 1-based
        self.assertIn("[2] other.pdf — đoạn 1", prompt)
        self.assertIn("some content", prompt)
        self.assertIn("what is X?", prompt)

    def test_full_text_flag_tells_model_it_has_everything(self):
        chunks = [{"document_id": "1", "original_name": "a", "chunk_index": 0, "text": "t"}]
        self.assertIn("toàn văn", rag.build_prompt("q", chunks, full_text=True))
        self.assertIn("không phải toàn văn", rag.build_prompt("q", chunks, full_text=False))

    def test_empty_chunks_uses_placeholder(self):
        prompt = rag.build_prompt("anything", [])
        self.assertIn("Không tìm thấy đoạn văn bản liên quan", prompt)


class CitedChunksTests(unittest.TestCase):
    chunks = [{"document_id": str(i), "chunk_index": i} for i in range(1, 7)]

    def refs(self, answer):
        return [number for number, _ in rag.cited_chunks(answer, self.chunks)]

    def test_parses_citation_styles_in_first_seen_order(self):
        self.assertEqual(self.refs("A [3]. B [1][3]. C [2, 5]. D [4-6]."), [3, 1, 2, 5, 4, 6])

    def test_ignores_out_of_range_and_non_citations(self):
        self.assertEqual(self.refs("See [0], [7], [99] and list[i] and [2]."), [2])

    def test_returns_matching_chunk(self):
        (number, chunk), = rag.cited_chunks("only [2]", self.chunks)
        self.assertEqual((number, chunk["document_id"]), (2, "2"))


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

    def _capture_gemini_payload(self, **ask_kwargs):
        os.environ["LLM_PROVIDER"] = "gemini"
        os.environ["LLM_API_KEY"] = "fake-key"
        sent = {}
        response = MagicMock()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *a: None
        response.read.return_value = b'{"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}'
        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data))
            return response
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            llm.ask("question", **ask_kwargs)
        return sent

    def test_gemini_sends_system_instruction_and_temperature(self):
        sent = self._capture_gemini_payload(system="be precise", temperature=0.2)
        self.assertEqual(sent["systemInstruction"], {"parts": [{"text": "be precise"}]})
        self.assertEqual(sent["generationConfig"]["temperature"], 0.2)
        self.assertNotIn("thinkingConfig", sent["generationConfig"])

    def test_gemini_thinking_budget_opt_in(self):
        os.environ["LLM_THINKING_BUDGET"] = "512"
        self.addCleanup(os.environ.pop, "LLM_THINKING_BUDGET", None)
        sent = self._capture_gemini_payload(temperature=0.2)
        self.assertEqual(sent["generationConfig"]["thinkingConfig"], {"thinkingBudget": 512})

    def test_gemini_thought_parts_are_not_part_of_the_answer(self):
        os.environ["LLM_PROVIDER"] = "gemini"
        os.environ["LLM_API_KEY"] = "fake-key"
        response = MagicMock()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *a: None
        response.read.return_value = json.dumps({"candidates": [{"content": {"parts": [
            {"text": "internal reasoning", "thought": True}, {"text": "final answer"},
        ]}}]}).encode()
        with patch("urllib.request.urlopen", return_value=response):
            answer, _ = llm.ask("q")
        self.assertEqual(answer, "final answer")

    def test_models_tried_sequentially_in_configured_order(self):
        os.environ["LLM_PROVIDER"] = "gemini"
        os.environ["LLM_API_KEY"] = "fake-key"
        os.environ["LLM_MODELS"] = "model-a,model-b,model-c"
        calls = []
        response = MagicMock()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *a: None
        response.read.return_value = b'{"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}'
        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url.split("/models/")[1].split(":")[0])
            if "model-a" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 503, "overloaded", {}, io.BytesIO(b"x"))
            return response
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _, model = llm.ask("q")
        # model-c is never called: no parallel racing burning its quota.
        self.assertEqual(calls, ["model-a", "model-b"])
        self.assertEqual(model, "model-b")


class DomainTests(unittest.TestCase):
    def setUp(self):
        domains._cached.cache_clear()
        self.addCleanup(domains._cached.cache_clear)

    def test_research_domain_loads(self):
        with patch.dict(os.environ, {"APP_DOMAIN": "research"}):
            domain = domains.current()
        self.assertEqual(domain.name, "research")
        self.assertTrue(domain.system)
        self.assertLessEqual(domain.temperature, 0.5)

    def test_missing_domain_falls_back_to_research(self):
        with patch.dict(os.environ, {"APP_DOMAIN": "does-not-exist"}):
            domain = domains.current()
        self.assertEqual(domain.name, "research")

    def test_canon_files_appended_to_system(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "demo"
            (folder / "canon").mkdir(parents=True)
            (folder / "system.md").write_text("Base rules.", encoding="utf-8")
            (folder / "domain.toml").write_text("temperature = 0.9\n[retrieval]\ntop_k = 3\n", encoding="utf-8")
            (folder / "canon" / "02-b.md").write_text("Second.", encoding="utf-8")
            (folder / "canon" / "01-a.md").write_text("First.", encoding="utf-8")
            with patch.object(domains, "_DOMAINS_DIR", Path(tmp)), patch.dict(os.environ, {"APP_DOMAIN": "demo"}):
                domain = domains.current()
        self.assertTrue(domain.system.startswith("Base rules."))
        self.assertLess(domain.system.index("First."), domain.system.index("Second."))
        self.assertEqual((domain.temperature, domain.top_k), (0.9, 3))


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

    def test_success_returns_answer_cited_sources_and_model(self):
        self.add_document("cloud.txt", "This document explains Docker and Cloud deployment.")
        with patch.object(llm, "ask", lambda prompt, **kw: ("Canned answer [1]", "fake-model")):
            result = service.ask_project(self.project_id, "What does it say about Docker?")
        self.assertEqual(result["answer"], "Canned answer [1]")
        self.assertEqual(result["model"], "fake-model")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["original_name"], "cloud.txt")
        self.assertEqual(result["sources"][0]["ref"], 1)

    def test_uncited_answer_has_no_sources(self):
        self.add_document("cloud.txt", "Docker content.")
        with patch.object(llm, "ask", lambda prompt, **kw: ("Tài liệu không đề cập.", "fake-model")):
            result = service.ask_project(self.project_id, "kubernetes?")
        self.assertEqual(result["sources"], [])

    def test_domain_system_and_temperature_sent_to_llm(self):
        self.add_document("cloud.txt", "Docker content.")
        received = {}
        def fake_ask(prompt, **kw):
            received.update(kw)
            return "answer", "m"
        with patch.dict(os.environ, {"APP_DOMAIN": "research"}), patch.object(llm, "ask", fake_ask):
            service.ask_project(self.project_id, "docker?")
        domain = domains.current()
        self.assertTrue(received["system"].startswith(domain.system))
        self.assertIn("Hôm nay là ngày", received["system"])
        self.assertEqual(received["temperature"], domain.temperature)

    def test_small_documents_sent_in_full_text(self):
        self.add_document("a.txt", "Alpha paragraph about docker.\n\nBeta paragraph about cooking.")
        received = {}
        def fake_ask(prompt, **kw):
            received["prompt"] = prompt
            return "ok", "m"
        with patch.object(llm, "ask", fake_ask):
            service.ask_project(self.project_id, "docker?")
        # Full-text mode: the unrelated paragraph is still there.
        self.assertIn("cooking", received["prompt"])
        self.assertIn("toàn văn các tài liệu", received["prompt"])

    def test_large_documents_use_bm25_retrieval(self):
        filler = "\n\n".join(f"Filler paragraph number {i} about gardening and weather." for i in range(40))
        self.add_document("big.txt", filler + "\n\nThe kubernetes cluster uses etcd for state.")
        received = {}
        def fake_ask(prompt, **kw):
            received["prompt"] = prompt
            return "ok", "m"
        small = domains.Domain(
            name="t", label="t", system="s", temperature=0.2, chunk_characters=200,
            chunk_overlap=0, top_k=2, full_text_max_chars=500, min_relative_score=0.25,
        )
        with patch.object(domains, "current", lambda: small), patch.object(llm, "ask", fake_ask):
            service.ask_project(self.project_id, "kubernetes etcd?")
        self.assertIn("etcd", received["prompt"])
        self.assertNotIn("number 0 ", received["prompt"])
        self.assertIn("không phải toàn văn", received["prompt"])

    def test_model_choice_passed_through_to_llm(self):
        self.add_document("cloud.txt", "Docker content.")
        received = {}
        def fake_ask(prompt, preferred_model=None, **kw):
            received["model"] = preferred_model
            return "answer", preferred_model
        with patch.object(llm, "ask", fake_ask):
            service.ask_project(self.project_id, "docker?", model="gemini-1.5-flash")
        self.assertEqual(received["model"], "gemini-1.5-flash")

    def test_llm_failure_returns_503(self):
        self.add_document("cloud.txt", "Docker and cloud content here.")
        def failing_ask(prompt, **kw):
            raise llm.LLMError("simulated failure")
        with patch.object(llm, "ask", failing_ask):
            with self.assertRaises(HTTPException) as caught:
                service.ask_project(self.project_id, "docker?")
        self.assertEqual(caught.exception.status_code, 503)

    def test_document_ids_scopes_answer_to_selected_sources(self):
        kept = self.add_document("cloud.txt", "Docker and cloud content here.")
        self.add_document("recipe.txt", "Docker and cloud content here.")
        received = {}
        def fake_ask(prompt, **kw):
            received["prompt"] = prompt
            return "ok [1]", "fake-model"
        with patch.object(llm, "ask", fake_ask):
            result = service.ask_project(self.project_id, "docker?", document_ids=[str(kept)])
        self.assertNotIn("recipe.txt", received["prompt"])
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["document_id"], str(kept))

    def test_empty_document_ids_rejected(self):
        self.add_document("cloud.txt", "Docker content.")
        with self.assertRaises(HTTPException) as caught:
            service.ask_project(self.project_id, "docker?", document_ids=[])
        self.assertEqual(caught.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
