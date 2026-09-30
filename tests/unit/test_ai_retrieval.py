"""Retrieval (app/rag.py): splitting documents into chunks, BM25 ranking,
the numbered prompt sent to the model and reading citations back out of the
answer. All pure functions — no storage, no network.
"""
import unittest

from app import rag


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


class StripCitationsTests(unittest.TestCase):
    def test_removes_markers_and_preceding_space(self):
        self.assertEqual(rag.strip_citations("Claim one [1][3]. Claim two [2, 4]."), "Claim one. Claim two.")



if __name__ == "__main__":
    unittest.main()
