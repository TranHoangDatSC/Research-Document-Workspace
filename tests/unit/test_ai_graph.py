"""Entity/relation graph extraction (app/graph.py): one LLM call per
document, turned into entities + relation triples for
app.rag.expand_by_entity_graph. The model is replaced directly
(app.llm.ask) — no network, no API key.
"""
import json
import unittest
from unittest.mock import patch

from app import graph, llm


class ExtractGraphTests(unittest.TestCase):
    def ask(self, answer):
        return patch.object(llm, "ask", lambda prompt, **kw: (answer, "fake-model"))

    def test_empty_text_returns_none_without_calling_the_model(self):
        with patch.object(llm, "ask") as mock_ask:
            self.assertIsNone(graph.extract_graph("   "))
        mock_ask.assert_not_called()

    def test_parses_valid_json(self):
        answer = '{"entities": ["A", "B"], "relations": [{"subject": "A", "relation": "advises", "object": "B"}]}'
        with self.ask(answer):
            result = graph.extract_graph("some text")
        self.assertEqual(result, {"entities": ["A", "B"], "relations": [{"subject": "A", "relation": "advises", "object": "B"}]})

    def test_strips_markdown_code_fence(self):
        answer = '```json\n{"entities": ["A"], "relations": []}\n```'
        with self.ask(answer):
            result = graph.extract_graph("text")
        self.assertEqual(result["entities"], ["A"])

    def test_unparseable_json_returns_none(self):
        with self.ask("not json at all"):
            self.assertIsNone(graph.extract_graph("text"))

    def test_unexpected_shape_returns_none(self):
        for answer in ('{"foo": "bar"}', '{"entities": "not a list", "relations": []}', "[]", '"just a string"'):
            with self.subTest(answer=answer), self.ask(answer):
                self.assertIsNone(graph.extract_graph("text"))

    def test_llm_failure_returns_none(self):
        with patch.object(llm, "ask", side_effect=llm.LLMError("simulated")):
            self.assertIsNone(graph.extract_graph("text"))

    def test_deduplicates_entities_preserving_first_order(self):
        answer = '{"entities": ["A", "B", "A"], "relations": []}'
        with self.ask(answer):
            result = graph.extract_graph("text")
        self.assertEqual(result["entities"], ["A", "B"])

    def test_drops_relations_missing_a_field(self):
        answer = '{"entities": [], "relations": [{"subject": "A", "relation": "x"}, {"subject": "A", "relation": "x", "object": "B"}]}'
        with self.ask(answer):
            result = graph.extract_graph("text")
        self.assertEqual(result["relations"], [{"subject": "A", "relation": "x", "object": "B"}])

    def test_non_string_entity_items_are_skipped(self):
        answer = '{"entities": ["A", 123, null, "B"], "relations": []}'
        with self.ask(answer):
            result = graph.extract_graph("text")
        self.assertEqual(result["entities"], ["A", "B"])

    def test_caps_entity_and_relation_counts(self):
        entities = [f"E{i}" for i in range(graph.MAX_ENTITIES + 10)]
        relations = [{"subject": "A", "relation": "r", "object": f"O{i}"} for i in range(graph.MAX_RELATIONS + 10)]
        with self.ask(json.dumps({"entities": entities, "relations": relations})):
            result = graph.extract_graph("text")
        self.assertEqual(len(result["entities"]), graph.MAX_ENTITIES)
        self.assertEqual(len(result["relations"]), graph.MAX_RELATIONS)

    def test_sends_temperature_zero_and_truncates_long_text(self):
        captured = {}

        def fake_ask(prompt, **kw):
            captured["kw"] = kw
            captured["prompt"] = prompt
            return '{"entities": [], "relations": []}', "fake-model"

        with patch.object(llm, "ask", fake_ask):
            graph.extract_graph("Z" * (graph.MAX_INPUT_CHARACTERS + 500))
        self.assertEqual(captured["kw"].get("temperature"), 0.0)
        sent_text = captured["prompt"].rsplit("VĂN BẢN:\n", 1)[1]
        self.assertEqual(sent_text, "Z" * graph.MAX_INPUT_CHARACTERS)


if __name__ == "__main__":
    unittest.main()
