"""AI question answering over a project's documents, and the chat history it
keeps: ask (saves the exchange), view, clear. Real routes and services;
storage from support.py, the model replaced by FakeLLM.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from support import FakeBackend

from fastapi import HTTPException

from app import access, domains, llm
from app.services import rag as rag_service

FILLER = "\n\n".join(f"Filler paragraph number {i} about gardening and weather." for i in range(40))
SMALL_DOMAIN = domains.Domain(
    name="test", label="test", system="s", temperature=0.2, chunk_characters=200,
    chunk_overlap=0, top_k=2, full_text_max_chars=500, min_relative_score=0.25,
)


class FakeLLM:
    """Replaces llm.ask: returns `answer` (or raises `error`), records every call."""

    def __init__(self, answer="Answer [1]", error=None):
        self.answer, self.error, self.calls = answer, error, []

    def __call__(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.error:
            raise llm.LLMError(self.error)
        return self.answer, kwargs.get("preferred_model") or "fake-model"

    @property
    def last(self):
        return self.calls[-1]


class ChatTestCase(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend().install(self)
        self.client = self.backend.client(self, role="user", username="alice")
        self.project_id = self.client.post("/projects", json={"name": "Research"}).json()["id"]
        self.llm = FakeLLM()
        patcher = patch.object(llm, "ask", self.llm)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_document(self, name, text, status="ready"):
        """A document whose text has already been extracted."""
        response = self.client.post(f"/projects/{self.project_id}/documents", files={"file": (name, b"x")})
        document_id = response.json()["id"]
        self.backend.details[document_id]["extracted_text"] = {"text": text} if text else None
        self.backend.documents[UUID(document_id)]["status"] = status
        return document_id

    def ask(self, question, **extra):
        return self.client.post(f"/projects/{self.project_id}/ask", json={"question": question, **extra})


class AskQuestionTests(ChatTestCase):
    def test_answer_with_cited_sources_and_model(self):
        self.add_document("cloud.txt", "This document explains Docker and Cloud deployment.")
        response = self.ask("What does it say about Docker?")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["answer"], body["model"]), ("Answer [1]", "fake-model"))
        (source,) = body["sources"]
        self.assertEqual((source["ref"], source["original_name"], source["chunk_index"]), (1, "cloud.txt", 0))

    def test_uncited_answer_lists_no_sources(self):
        self.add_document("cloud.txt", "Docker content.")
        self.llm.answer = "Tài liệu không đề cập."
        self.assertEqual(self.ask("kubernetes?").json()["sources"], [])

    def test_domain_rules_sent_with_every_question(self):
        self.add_document("cloud.txt", "Docker content.")
        with patch.dict(os.environ, {"APP_DOMAIN": "research"}):
            self.ask("docker?")
            domain = domains.current()
        self.assertTrue(self.llm.last["system"].startswith(domain.system))
        self.assertIn("Hôm nay là ngày", self.llm.last["system"])
        self.assertEqual(self.llm.last["temperature"], domain.temperature)

    def test_small_documents_are_sent_in_full(self):
        self.add_document("a.txt", "Alpha paragraph about docker.\n\nBeta paragraph about cooking.")
        self.ask("docker?")
        self.assertIn("cooking", self.llm.last["prompt"])  # unrelated paragraph still there
        self.assertIn("toàn văn các tài liệu", self.llm.last["prompt"])

    def test_large_documents_send_only_relevant_passages(self):
        self.add_document("big.txt", FILLER + "\n\nThe kubernetes cluster uses etcd for state.")
        with patch.object(domains, "current", lambda: SMALL_DOMAIN):
            self.ask("kubernetes etcd?")
        self.assertIn("etcd", self.llm.last["prompt"])
        self.assertNotIn("number 0 ", self.llm.last["prompt"])
        self.assertIn("không phải toàn văn", self.llm.last["prompt"])

    def test_entity_graph_pulls_in_related_document_across_a_relation(self):
        # Full integration: storage (entity_graph on one document) -> service
        # (ask_project merges it project-wide) -> retrieval (expand_by_entity_graph,
        # app/rag.py) -> the final prompt. BM25 alone would never connect these
        # two documents: they share no words, only a stored relation.
        advisor_id = self.add_document("advisor.txt", FILLER + "\n\nKhiet is the advisor who reviews many student projects every year.")
        self.add_document("student.txt", FILLER + "\n\nTien enrolled in the information technology program this year.")
        self.backend.details[advisor_id]["entity_graph"] = {
            "entities": ["Khiet", "Tien"],
            "relations": [{"subject": "Tien", "relation": "advised by", "object": "Khiet"}],
        }
        with patch.object(domains, "current", lambda: SMALL_DOMAIN):
            self.ask("Who is the advisor?")
        self.assertIn("Khiet", self.llm.last["prompt"])
        self.assertIn("Tien", self.llm.last["prompt"])  # pulled in via the relation, not keyword overlap

    def test_selected_documents_only(self):
        kept = self.add_document("cloud.txt", "Docker and cloud content here.")
        self.add_document("recipe.txt", "Docker and cloud content here.")
        body = self.ask("docker?", document_ids=[kept]).json()
        self.assertNotIn("recipe.txt", self.llm.last["prompt"])
        self.assertEqual([s["document_id"] for s in body["sources"]], [kept])

    def test_documents_not_ready_or_not_extracted_are_skipped(self):
        self.add_document("pending.txt", "Docker content.", status="pending")
        self.add_document("raw.txt", None)
        self.assertEqual(self.ask("docker?").status_code, 409)
        self.assertEqual(self.llm.calls, [])

    def test_chosen_model_is_passed_through(self):
        self.add_document("cloud.txt", "Docker content.")
        self.assertEqual(self.ask("docker?", model="gemini-x").json()["model"], "gemini-x")

    def test_invalid_questions(self):
        self.add_document("cloud.txt", "Docker content.")
        self.assertEqual(self.ask("   ").status_code, 422)
        self.assertEqual(self.ask("x" * 2001).status_code, 422)
        self.assertEqual(self.ask("docker?", document_ids=[]).status_code, 422)
        self.assertEqual(self.client.post(f"/projects/{uuid4()}/ask", json={"question": "q"}).status_code, 404)
        self.assertEqual(self.llm.calls, [])

    def test_model_failure_is_503(self):
        self.add_document("cloud.txt", "Docker content.")
        self.llm.error = "simulated failure"
        response = self.ask("docker?")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.backend.chats, [])  # a failed question is not saved


class SaveChatHistoryTests(ChatTestCase):
    def test_exchange_saved_and_sent_back_next_turn(self):
        self.add_document("cloud.txt", "Docker content.")
        self.llm.answer = "First answer [1]."
        self.ask("first question")
        self.assertEqual([(m["role"], m["content"]) for m in self.backend.chats], [("user", "first question"), ("model", "First answer [1].")])
        self.assertEqual(self.backend.chats[1]["sources"][0]["ref"], 1)

        self.ask("follow up")
        # Old citation numbers stripped: passages are renumbered every turn.
        self.assertEqual(self.llm.last["history"], [("user", "first question"), ("model", "First answer.")])

    def test_long_old_answer_is_clipped(self):
        self.add_document("cloud.txt", "Docker content.")
        self.llm.answer = "x" * (rag_service.HISTORY_MESSAGE_MAX_CHARS + 500)
        self.ask("q1")
        self.ask("q2")
        self.assertLessEqual(len(self.llm.last["history"][1][1]), rag_service.HISTORY_MESSAGE_MAX_CHARS + 2)

    def test_follow_up_retrieval_uses_previous_question(self):
        self.add_document("big.txt", FILLER + "\n\nThe kubernetes cluster uses etcd for state.")
        with patch.object(domains, "current", lambda: SMALL_DOMAIN):
            self.ask("How does kubernetes store state?")
            self.ask("explain that further")
        self.assertIn("etcd", self.llm.last["prompt"])

    def test_history_store_down_still_answers(self):
        self.add_document("cloud.txt", "Docker content.")

        def down(*args):
            raise RuntimeError("simulated chat store failure")

        with patch.object(rag_service.chats_repository, "list_messages", down), \
             patch.object(rag_service.chats_repository, "add_messages", down):
            response = self.ask("docker?")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.llm.last["history"], [])

    def test_service_without_user_keeps_no_history(self):
        self.add_document("cloud.txt", "Docker content.")
        # Outside a request: act as the owner (projects are private), but pass no
        # user_id for history -> stateless.
        with access.acting_as(self.client.user["id"]):
            rag_service.ask_project(UUID(self.project_id), "q")
        self.assertEqual(self.backend.chats, [])

    def test_service_with_nobody_bound_refuses(self):
        with self.assertRaises(HTTPException) as caught:
            rag_service.ask_project(UUID(self.project_id), "q")
        self.assertEqual(caught.exception.status_code, 401)


class ViewChatHistoryTests(ChatTestCase):
    def test_api_returns_my_conversation_and_hides_it_from_others(self):
        self.add_document("cloud.txt", "Docker content.")
        self.ask("my question")
        other = self.backend.client(self, role="user", username="bob")
        self.assertEqual([m["content"] for m in self.client.get(f"/projects/{self.project_id}/chat").json()["messages"]], ["my question", "Answer [1]"])
        self.assertEqual(other.get(f"/projects/{self.project_id}/chat").status_code, 404)  # not bob's project

    def test_project_page_shows_the_conversation(self):
        self.add_document("paper.pdf", "Docker content.")
        self.client.post(f"/ui/projects/{self.project_id}/ask", data={"question": "Câu hỏi <script>"}, follow_redirects=False)
        html = self.client.get(f"/ui/projects/{self.project_id}").text
        self.assertIn("Câu hỏi &lt;script&gt;", html)  # escaped, not injected
        self.assertIn('class="md-source"', html)       # answer upgraded to markdown by app.js
        self.assertIn("[1] paper.pdf · đoạn 1", html)
        self.assertIn("Mô hình: fake-model", html)

    def test_form_ask_redirects_back_to_the_chat(self):
        self.add_document("cloud.txt", "Docker content.")
        response = self.client.post(f"/ui/projects/{self.project_id}/ask", data={"question": "q"}, follow_redirects=False)
        self.assertEqual((response.status_code, response.headers["location"]), (303, f"/ui/projects/{self.project_id}#ai-panel"))


class ClearChatHistoryTests(ChatTestCase):
    """Deleting one thread (not "all history" — a project can hold several)."""

    def test_others_can_neither_ask_nor_clear(self):
        self.add_document("cloud.txt", "Docker content.")
        chat_id = self.ask("mine").json()["chat_id"]
        other = self.backend.client(self, role="user", username="bob")
        self.assertEqual(other.post(f"/projects/{self.project_id}/ask", json={"question": "bob's"}).status_code, 404)
        self.assertEqual(other.delete(f"/projects/{self.project_id}/chat", params={"chat_id": chat_id}).status_code, 404)
        self.assertEqual(len(self.backend.chats), 2)
        self.assertEqual(self.client.delete(f"/projects/{self.project_id}/chat", params={"chat_id": chat_id}).status_code, 204)
        self.assertEqual(self.backend.chats, [])

    def test_delete_requires_a_chat_id(self):
        self.assertEqual(self.client.delete(f"/projects/{self.project_id}/chat").status_code, 422)

    def test_confirm_page_then_delete_form_redirects_to_project(self):
        self.add_document("cloud.txt", "Docker content.")
        chat_id = self.ask("q").json()["chat_id"]
        confirm = self.client.get(f"/ui/projects/{self.project_id}/chat/{chat_id}/delete")
        self.assertEqual(confirm.status_code, 200)
        self.assertIn("q", confirm.text)
        response = self.client.post(f"/ui/projects/{self.project_id}/chat/{chat_id}/delete", data={"confirm": "delete"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.backend.chats, [])

    def test_delete_form_without_confirmation_is_422(self):
        self.add_document("cloud.txt", "Docker content.")
        chat_id = self.ask("q").json()["chat_id"]
        self.assertEqual(self.client.post(f"/ui/projects/{self.project_id}/chat/{chat_id}/delete").status_code, 422)
        self.assertEqual(len(self.backend.chats), 2)

    def test_confirm_page_for_unknown_chat_is_404(self):
        self.assertEqual(self.client.get(f"/ui/projects/{self.project_id}/chat/{uuid4()}/delete").status_code, 404)

    def test_clear_when_store_down_is_503(self):
        self.backend.fail.add("mongo")
        self.assertEqual(self.client.delete(f"/projects/{self.project_id}/chat", params={"chat_id": "whatever"}).status_code, 503)


class MultipleChatThreadsTests(ChatTestCase):
    """A project can hold several independent conversations per user."""

    def test_omitting_chat_id_continues_the_most_recent_thread(self):
        self.add_document("cloud.txt", "Docker content.")
        first = self.ask("q1").json()
        second = self.ask("q2").json()
        self.assertEqual(first["chat_id"], second["chat_id"])

    def test_a_fresh_chat_id_starts_an_isolated_thread(self):
        self.add_document("cloud.txt", "Docker content.")
        old_chat_id = self.ask("about the old topic").json()["chat_id"]
        new_chat_id = str(uuid4())
        self.ask("about a new topic", chat_id=new_chat_id)
        self.assertNotEqual(old_chat_id, new_chat_id)
        # The new thread's history has no memory of the old one's question.
        self.ask("another one", chat_id=new_chat_id)
        self.assertEqual(self.llm.last["history"], [("user", "about a new topic"), ("model", "Answer")])

    def test_list_chats_returns_both_threads_newest_first_with_previews(self):
        self.add_document("cloud.txt", "Docker content.")
        self.ask("first thread question")
        second_id = str(uuid4())
        self.ask("second thread question", chat_id=second_id)
        chats = self.client.get(f"/projects/{self.project_id}/chats").json()["chats"]
        self.assertEqual(len(chats), 2)
        self.assertEqual(chats[0]["chat_id"], second_id)
        self.assertEqual(chats[0]["preview"], "second thread question")
        self.assertEqual(chats[1]["preview"], "first thread question")

    def test_get_chat_fetches_one_specific_thread(self):
        self.add_document("cloud.txt", "Docker content.")
        first_id = self.ask("first thread question").json()["chat_id"]
        second_id = str(uuid4())
        self.ask("second thread question", chat_id=second_id)
        body = self.client.get(f"/projects/{self.project_id}/chat", params={"chat_id": first_id}).json()
        self.assertEqual(body["chat_id"], first_id)
        self.assertEqual([m["content"] for m in body["messages"]], ["first thread question", "Answer [1]"])

    def test_deleting_one_thread_leaves_the_other_intact(self):
        self.add_document("cloud.txt", "Docker content.")
        keep_id = self.ask("keep this one").json()["chat_id"]
        delete_id = str(uuid4())
        self.ask("delete this one", chat_id=delete_id)
        self.assertEqual(self.client.delete(f"/projects/{self.project_id}/chat", params={"chat_id": delete_id}).status_code, 204)
        remaining = self.client.get(f"/projects/{self.project_id}/chats").json()["chats"]
        self.assertEqual([c["chat_id"] for c in remaining], [keep_id])

    def test_project_page_offers_a_new_chat_sentinel(self):
        self.add_document("cloud.txt", "Docker content.")
        self.ask("an existing question")
        html = self.client.get(f"/ui/projects/{self.project_id}?chat_id=new").text
        # The old question still shows up in the switcher's history list...
        self.assertIn("an existing question", html)
        # ...but not as the active thread, which is empty.
        active_thread = html.split('id="ai-thread"')[1].split('id="ai-form"')[0]
        self.assertNotIn("an existing question", active_thread)
        self.assertIn('id="ai-chat-id-input"', html)


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
            self.assertEqual(domains.current().name, "research")

    def test_config_and_canon_files_loaded(self):
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
        self.assertLess(domain.system.index("First."), domain.system.index("Second."))  # file-name order
        self.assertEqual((domain.temperature, domain.top_k), (0.9, 3))


if __name__ == "__main__":
    unittest.main()
