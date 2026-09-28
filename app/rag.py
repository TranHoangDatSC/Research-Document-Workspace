"""Lexical retrieval over already-extracted document text (Day 5's
`extracted_text`). No embeddings, no vector DB: keyword-overlap scoring is
"RAG thu nhỏ" — good enough for a handful of short research documents per
project, and needs no extra dependency or paid API for the retrieval step
itself (only the final answer step calls an LLM, see app/llm.py).
"""
import re

CHUNK_CHARACTERS = 1000
CHUNK_OVERLAP = 100
TOP_K = 6

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text):
    return set(_WORD_RE.findall(text.lower()))


def chunk_text(text, chunk_characters=CHUNK_CHARACTERS, overlap=CHUNK_OVERLAP):
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_characters, length)
        chunks.append(text[start:end])
        if end == length:
            break
        start = end - overlap
    return chunks


def build_chunks(documents):
    """documents: iterable of {"document_id", "original_name", "extracted_text"}
    where extracted_text is the app.extractors result dict (or None/missing).
    Returns a flat list of {"document_id", "original_name", "chunk_index", "text"}.
    """
    chunks = []
    for doc in documents:
        extracted = doc.get("extracted_text")
        if not extracted or not extracted.get("text"):
            continue
        for index, piece in enumerate(chunk_text(extracted["text"])):
            chunks.append({
                "document_id": doc["document_id"],
                "original_name": doc["original_name"],
                "chunk_index": index,
                "text": piece,
            })
    return chunks


def rank_chunks(question, chunks, top_k=TOP_K):
    """Keyword-overlap scoring: cheap, deterministic, no embeddings/API call."""
    question_words = _tokenize(question)
    if not question_words:
        return []
    scored = []
    for chunk in chunks:
        overlap = len(question_words & _tokenize(chunk["text"]))
        if overlap:
            scored.append((overlap, chunk))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]]


def build_prompt(question, ranked_chunks):
    if not ranked_chunks:
        context = "(Không tìm thấy đoạn văn bản liên quan trong tài liệu đã trích xuất.)"
    else:
        context = "\n\n".join(
            f"[Tài liệu: {chunk['original_name']} - đoạn {chunk['chunk_index'] + 1}]\n{chunk['text']}"
            for chunk in ranked_chunks
        )
    return (
        "Bạn là trợ lý đọc tài liệu nghiên cứu. Chỉ trả lời dựa trên nội dung "
        "trong phần TÀI LIỆU dưới đây. Nếu không tìm thấy thông tin liên quan, "
        "hãy nói rõ là không có trong tài liệu, không tự suy đoán thêm.\n\n"
        f"TÀI LIỆU:\n{context}\n\nCÂU HỎI: {question}"
    )
