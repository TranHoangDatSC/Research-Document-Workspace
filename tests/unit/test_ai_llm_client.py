"""LLM client (app/llm.py): configuration, request payload, response parsing
and model/key rotation. The HTTP call is replaced by FakeLLMServer — no
network, no API key.
"""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from app import llm

GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeLLMServer:
    """Stands in for urllib.request.urlopen. `reply(request)` returns a dict
    (sent back as JSON) or raises; every request is recorded."""

    def __init__(self, reply=lambda request: GEMINI_OK):
        self.reply = reply
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return FakeResponse(json.dumps(self.reply(request)).encode())

    @property
    def payloads(self):
        return [json.loads(r.data) for r in self.requests]

    @property
    def models(self):
        return [r.full_url.split("/models/")[1].split(":")[0] for r in self.requests]


def http_error(request, code):
    return urllib.error.HTTPError(request.full_url, code, "error", {}, io.BytesIO(b"error body"))


class LLMTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "LLM_API_KEY": "fake-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("LLM_API_KEYS", "LLM_MODEL", "LLM_MODELS", "LLM_THINKING_BUDGET"):
            os.environ.pop(key, None)

    def ask(self, server, *args, **kwargs):
        with patch("urllib.request.urlopen", side_effect=server):
            return llm.ask(*args, **kwargs)


class ConfigurationTests(LLMTestCase):
    def test_missing_key_or_provider(self):
        for key in ("LLM_PROVIDER", "LLM_API_KEY"):
            with self.subTest(missing=key), patch.dict(os.environ, {key: ""}):
                with self.assertRaises(llm.LLMError):
                    llm.ask("hello")

    def test_unsupported_provider(self):
        os.environ["LLM_PROVIDER"] = "carrier-pigeon"
        with self.assertRaises(llm.LLMError):
            llm.ask("hello")


class RequestPayloadTests(LLMTestCase):
    def test_gemini_sends_system_instruction_and_temperature(self):
        server = FakeLLMServer()
        self.ask(server, "question", system="be precise", temperature=0.2)
        (payload,) = server.payloads
        self.assertEqual(payload["systemInstruction"], {"parts": [{"text": "be precise"}]})
        self.assertEqual(payload["generationConfig"], {"temperature": 0.2})

    def test_thinking_budget_is_opt_in(self):
        os.environ["LLM_THINKING_BUDGET"] = "512"
        server = FakeLLMServer()
        self.ask(server, "q", temperature=0.2)
        self.assertEqual(server.payloads[0]["generationConfig"]["thinkingConfig"], {"thinkingBudget": 512})

    def test_gemini_history_precedes_new_question(self):
        server = FakeLLMServer()
        self.ask(server, "new question", history=[("user", "old q"), ("model", "old a")])
        self.assertEqual(
            [(c["role"], c["parts"][0]["text"]) for c in server.payloads[0]["contents"]],
            [("user", "old q"), ("model", "old a"), ("user", "new question")],
        )

    def test_openai_uses_system_and_assistant_roles(self):
        os.environ["LLM_PROVIDER"] = "openai"
        server = FakeLLMServer(lambda r: {"choices": [{"message": {"content": "ok"}}]})
        self.ask(server, "new question", system="sys", temperature=0.2, history=[("user", "old q"), ("model", "old a")])
        payload = server.payloads[0]
        self.assertEqual(
            [(m["role"], m["content"]) for m in payload["messages"]],
            [("system", "sys"), ("user", "old q"), ("assistant", "old a"), ("user", "new question")],
        )
        self.assertEqual(payload["temperature"], 0.2)


class HistoryNormalizationTests(unittest.TestCase):
    def test_drops_empty_and_leading_model_turns_and_merges_same_role(self):
        turns = llm._normalize_history([
            ("model", "orphan answer"), ("user", "q1"), ("user", "q1 again"),
            ("model", ""), ("model", "a1"), ("user", "   "), ("user", "q2"), ("model", "a2"),
        ])
        self.assertEqual(turns, [("user", "q1\n\nq1 again"), ("model", "a1"), ("user", "q2"), ("model", "a2")])

    def test_trailing_user_turn_dropped(self):
        self.assertEqual(llm._normalize_history([("user", "q"), ("model", "a"), ("user", "dangling")]), [("user", "q"), ("model", "a")])

    def test_none_is_empty(self):
        self.assertEqual(llm._normalize_history(None), [])


class ResponseParsingTests(LLMTestCase):
    def test_gemini_answer(self):
        server = FakeLLMServer(lambda r: {"candidates": [{"content": {"parts": [{"text": "Gemini answer"}]}}]})
        answer, model = self.ask(server, "hello")
        self.assertEqual(answer, "Gemini answer")
        self.assertTrue(model)

    def test_openai_answer(self):
        os.environ["LLM_PROVIDER"] = "openai"
        answer, _ = self.ask(FakeLLMServer(lambda r: {"choices": [{"message": {"content": "OpenAI answer"}}]}), "hello")
        self.assertEqual(answer, "OpenAI answer")

    def test_thought_parts_are_not_part_of_the_answer(self):
        server = FakeLLMServer(lambda r: {"candidates": [{"content": {"parts": [
            {"text": "internal reasoning", "thought": True}, {"text": "final answer"},
        ]}}]})
        answer, _ = self.ask(server, "q")
        self.assertEqual(answer, "final answer")

    def test_malformed_or_empty_response_is_an_error(self):
        for body in ({"unexpected": "shape"}, {"candidates": [{"content": {"parts": [{"text": " "}]}, "finishReason": "SAFETY"}]}):
            with self.subTest(body=body), self.assertRaises(llm.LLMError):
                self.ask(FakeLLMServer(lambda r: body), "hello")


class RotationTests(LLMTestCase):
    def test_overloaded_model_falls_through_to_next_in_order(self):
        os.environ["LLM_MODELS"] = "model-a,model-b,model-c"

        def reply(request):
            if "model-a" in request.full_url:
                raise http_error(request, 503)
            return GEMINI_OK

        server = FakeLLMServer(reply)
        _, model = self.ask(server, "q")
        self.assertEqual(model, "model-b")
        # model-c never called: one model at a time, not raced in parallel.
        self.assertEqual(server.models, ["model-a", "model-b"])

    def test_preferred_model_tried_first(self):
        os.environ["LLM_MODELS"] = "model-a,model-b,model-c"
        server = FakeLLMServer()
        _, model = self.ask(server, "q", preferred_model="model-c")
        self.assertEqual((model, server.models), ("model-c", ["model-c"]))

    def test_rate_limited_key_falls_through_to_next_key(self):
        os.environ["LLM_API_KEYS"] = "bad-key,good-key"
        os.environ["LLM_MODELS"] = "model-a"

        def reply(request):
            if "key=bad-key" in request.full_url:
                raise http_error(request, 429)
            return {"candidates": [{"content": {"parts": [{"text": "ok with good key"}]}}]}

        answer, _ = self.ask(FakeLLMServer(reply), "q")
        self.assertEqual(answer, "ok with good key")

    def test_every_combination_failing_reports_attempts(self):
        os.environ["LLM_API_KEYS"] = "key1,key2"
        os.environ["LLM_MODELS"] = "model-a,model-b"

        def reply(request):
            raise http_error(request, 503)

        with self.assertRaises(llm.LLMError) as caught:
            self.ask(FakeLLMServer(reply), "q")
        self.assertIn("4", caught.exception.message)  # 2 keys x 2 models


if __name__ == "__main__":
    unittest.main()
