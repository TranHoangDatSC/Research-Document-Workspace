"""Retrieval over already-extracted document text (Day 5's `extracted_text`).
No embeddings, no vector DB — two pure-Python strategies, picked per question
by app/services/rag.py:

- full text: when the selected documents are small enough (domain setting
  `full_text_max_chars`), every chunk goes to the model. With a handful of
  short research documents this beats any ranking — nothing relevant can be
  missed.
- BM25: otherwise, chunks are ranked with Okapi BM25 over diacritic-folded
  tokens (Vietnamese stopwords removed) plus adjacent-syllable bigrams, since
  Vietnamese words are mostly two syllables ("học sinh", "chính trị") and a
  bag of single syllables loses that. `expand_by_shared_terms` then adds a
  graph-lite hop on top: chunks connected to a BM25 hit through a shared rare
  term (a name, a project title) get pulled in too, even without matching the
  question's own words — see its docstring for the no-infrastructure design
  and where a real entity/relation graph would plug in later.

Chunks are numbered [1]..[n] in the prompt; the model cites those numbers and
`cited_chunks` maps them back, so the sources shown are the ones actually used.
"""
import math
import re
import unicodedata
from collections import Counter

CHUNK_CHARACTERS = 1200
CHUNK_OVERLAP = 150
TOP_K = 8
MIN_RELATIVE_SCORE = 0.25

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
_SENTENCE_RE = re.compile(r"(?<=[.!?…;:])\s+")
_CITATION_RE = re.compile(r"\[(\d+(?:\s*[,;–-]\s*\d+)*)\]")

# Diacritic-folded (see _fold): "của" -> "cua". Function words only — anything
# that could be a content word in some context stays searchable.
_STOPWORDS = frozenset("""
va la cua cac nhung mot nhu cho voi trong thi ma co duoc nay do de khi tu tai
ve se da dang bi boi hay hoac nen vi ra len vao cung rat lai con theo nao gi
sao the nhieu it moi toi ban chung ta ay kia day neu ma nha hon nhat tren duoi
giua sau truoc cung khong chi van dieu viec cai chiec nhu vay thuoc
a an the of to in is are was were be been and or for on with at by from as
it this that these those what which who how why does do did about into than
""".split())


def _fold(text):
    """Lowercase, strip diacritics, đ -> d: lets "hoc sinh" match "học sinh"."""
    text = unicodedata.normalize("NFD", text.lower()).replace("đ", "d")
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def tokenize(text):
    """Terms for BM25: non-stopword syllables + bigrams of adjacent syllables
    (a bigram is kept unless both halves are stopwords)."""
    words = _WORD_RE.findall(_fold(text))
    terms = [w for w in words if w not in _STOPWORDS]
    for left, right in zip(words, words[1:]):
        if left not in _STOPWORDS or right not in _STOPWORDS:
            terms.append(f"{left}_{right}")
    return terms


def _units(text, limit):
    """Paragraphs; a paragraph longer than `limit` is split into sentences,
    and a sentence still longer than `limit` is hard-cut."""
    units = []
    for paragraph in _PARAGRAPH_RE.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            units.append(paragraph)
            continue
        for sentence in _SENTENCE_RE.split(paragraph):
            sentence = sentence.strip()
            while len(sentence) > limit:
                units.append(sentence[:limit])
                sentence = sentence[limit:]
            if sentence:
                units.append(sentence)
    return units


def chunk_text(text, chunk_characters=CHUNK_CHARACTERS, overlap=CHUNK_OVERLAP):
    """Packs whole paragraphs/sentences into chunks of at most
    `chunk_characters`, never cutting mid-sentence unless a single sentence is
    longer than a chunk. Consecutive chunks share trailing units of up to
    `overlap` characters so an idea split across the boundary survives."""
    text = text.strip()
    if not text:
        return []
    chunks = []
    current, size = [], 0
    for unit in _units(text, chunk_characters):
        if current and size + len(unit) > chunk_characters:
            chunks.append("\n".join(current))
            kept, kept_size = [], 0
            for previous in reversed(current):
                if kept_size + len(previous) + 1 > overlap:
                    break
                kept.insert(0, previous)
                kept_size += len(previous) + 1
            if kept_size + len(unit) > chunk_characters:
                kept, kept_size = [], 0
            current, size = kept, kept_size
        current.append(unit)
        size += len(unit) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_chunks(documents, chunk_characters=CHUNK_CHARACTERS, overlap=CHUNK_OVERLAP):
    """documents: iterable of {"document_id", "original_name", "extracted_text"}
    where extracted_text is the app.extractors result dict (or None/missing).
    Returns a flat list of {"document_id", "original_name", "chunk_index", "text"}.
    """
    chunks = []
    for doc in documents:
        extracted = doc.get("extracted_text")
        if not extracted or not extracted.get("text"):
            continue
        for index, piece in enumerate(chunk_text(extracted["text"], chunk_characters, overlap)):
            chunks.append({
                "document_id": doc["document_id"],
                "original_name": doc["original_name"],
                "chunk_index": index,
                "text": piece,
            })
    return chunks


def rank_chunks(question, chunks, top_k=TOP_K, min_relative_score=MIN_RELATIVE_SCORE, k1=1.5, b=0.75):
    """Okapi BM25. Chunks scoring below `min_relative_score` × the best score
    are dropped, so a one-word coincidental match doesn't pad the context."""
    query = set(tokenize(question))
    if not query or not chunks:
        return []
    docs = [Counter(tokenize(chunk["text"])) for chunk in chunks]
    total = len(docs)
    average_length = sum(sum(d.values()) for d in docs) / total or 1
    document_frequency = Counter(term for d in docs for term in query if term in d)

    scored = []
    for chunk, terms in zip(chunks, docs):
        length = sum(terms.values())
        score = 0.0
        for term in query:
            frequency = terms.get(term)
            if not frequency:
                continue
            df = document_frequency[term]
            idf = math.log((total - df + 0.5) / (df + 0.5) + 1)
            score += idf * frequency * (k1 + 1) / (frequency + k1 * (1 - b + b * length / average_length))
        if score > 0:
            scored.append((score, chunk))
    if not scored:
        return []
    scored.sort(key=lambda pair: pair[0], reverse=True)
    floor = scored[0][0] * min_relative_score
    return [chunk for score, chunk in scored[:top_k] if score >= floor]


def _chunk_key(chunk):
    return (chunk["document_id"], chunk["chunk_index"])


def _rare_terms(term_lists, max_chunk_fraction=0.3, max_chunks=3):
    """Terms present in only a few chunks. In a small corpus a word repeated
    everywhere ("nghiên cứu", "dữ liệu"...) says nothing about which chunks
    belong together, but a name, a project title or a place that recurs in
    just a couple of chunks is a real signal connecting them — the same
    intuition behind TF-IDF/BM25's own idf term, reused here as a graph edge
    filter instead of a ranking weight."""
    total = len(term_lists)
    if not total:
        return set()
    counts = Counter()
    for terms in term_lists:
        counts.update(set(terms))
    limit = max(1, min(max_chunks, int(total * max_chunk_fraction)))
    return {term for term, n in counts.items() if n <= limit}


def expand_by_shared_terms(selected, chunks, max_extra=2):
    """Graph-lite retrieval augmentation: no entities, no embeddings, no extra
    LLM call — just the existing BM25 term index read as an implicit graph (a
    rare term is an edge; a chunk is a node) and walked one hop out from the
    chunks BM25 already selected. Pulls in chunks that share a specific
    name/title/place with a selected chunk even when they share no words
    with the QUESTION itself — the multi-hop case plain keyword overlap
    misses ("tài liệu nào liên quan đến đề tài do X hướng dẫn?" when the
    connecting chunk never mentions X, only the shared project name does).

    A real entity/relation graph (extracted once per document — e.g. one LLM
    call at extraction time, stored in MongoDB) would replace the "rare term"
    signal below with actual named entities and typed relations; this
    function is where that would plug in, the one-hop walk stays the same.
    """
    if not selected or max_extra <= 0 or len(selected) >= len(chunks):
        return selected
    terms_by_key = {_chunk_key(c): tokenize(c["text"]) for c in chunks}
    rare = _rare_terms(terms_by_key.values())
    if not rare:
        return selected

    selected_keys = {_chunk_key(c) for c in selected}
    anchor_terms = set()
    for chunk in selected:
        anchor_terms |= set(terms_by_key[_chunk_key(chunk)]) & rare
    if not anchor_terms:
        return selected

    candidates = []
    for chunk in chunks:
        key = _chunk_key(chunk)
        if key in selected_keys:
            continue
        shared = anchor_terms & set(terms_by_key[key])
        if shared:
            candidates.append((len(shared), chunk))
    if not candidates:
        return selected
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return selected + [chunk for _, chunk in candidates[:max_extra]]


def order_for_reading(chunks):
    """Document order (by first appearance), then chunk order within a
    document — passages read as continuous text instead of shuffled by score."""
    first_seen = {}
    for chunk in chunks:
        first_seen.setdefault(chunk["document_id"], len(first_seen))
    return sorted(chunks, key=lambda c: (first_seen[c["document_id"]], c["chunk_index"]))


def build_prompt(question, numbered_chunks, full_text=False):
    """User turn: numbered passages + the question. The rules for using them
    live in the domain's system instruction (app/domains/<name>/system.md)."""
    if not numbered_chunks:
        context = "(Không tìm thấy đoạn văn bản liên quan trong tài liệu đã trích xuất.)"
    else:
        context = "\n\n".join(
            f"[{number}] {chunk['original_name']} — đoạn {chunk['chunk_index'] + 1}\n{chunk['text']}"
            for number, chunk in enumerate(numbered_chunks, start=1)
        )
    scope = (
        "toàn văn các tài liệu đã chọn" if full_text
        else "các đoạn liên quan nhất được trích từ tài liệu đã chọn (không phải toàn văn)"
    )
    return f"TÀI LIỆU ({scope}):\n\n{context}\n\n---\nCÂU HỎI: {question}"


def strip_citations(text):
    """Removes [n]-style citation markers (and the space before them)."""
    return re.sub(r"\s*" + _CITATION_RE.pattern, "", text)


def cited_chunks(answer, numbered_chunks):
    """Chunks the answer actually cites via [n], [n][m], [n, m] or [n-m], in
    first-citation order. Out-of-range numbers are ignored."""
    seen = []
    for match in _CITATION_RE.finditer(answer):
        numbers = []
        for part in re.split(r"\s*[,;]\s*", match.group(1)):
            bounds = re.split(r"\s*[–-]\s*", part)
            if len(bounds) == 2:
                low, high = int(bounds[0]), int(bounds[1])
                if 0 < high - low < 50:
                    numbers.extend(range(low, high + 1))
                    continue
            numbers.append(int(bounds[0]))
        for number in numbers:
            if 1 <= number <= len(numbered_chunks) and number not in seen:
                seen.append(number)
    return [(number, numbered_chunks[number - 1]) for number in seen]
