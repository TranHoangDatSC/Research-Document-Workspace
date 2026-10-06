"""Retrieval over extracted document text. Pure Python: no embeddings, no
vector DB. services/rag.py picks one of two modes per question:

- Full text: small selections (<= domain `full_text_max_chars`) are sent
  whole, so nothing relevant can be missed.
- BM25: otherwise chunks are ranked by Okapi BM25 over diacritic-folded
  syllables plus adjacent-syllable bigrams (most Vietnamese words are two
  syllables). Then one graph hop adds related chunks that don't share the
  question's words:
    - expand_by_entity_graph when a document has an LLM-built entity graph
      (app/graph.py);
    - expand_by_shared_terms otherwise, using rare shared terms as edges.

Chunks are numbered [1]..[n] in the prompt; `cited_chunks` maps the model's
citations back so only the sources actually used are shown.
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

# Diacritic-folded (see _fold). Function words only.
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
    """Terms found in only a few chunks: a recurring name or title links
    chunks, a word that appears everywhere does not (the idea behind idf)."""
    total = len(term_lists)
    if not total:
        return set()
    counts = Counter()
    for terms in term_lists:
        counts.update(set(terms))
    limit = max(1, min(max_chunks, int(total * max_chunk_fraction)))
    return {term for term, n in counts.items() if n <= limit}


def expand_by_shared_terms(selected, chunks, max_extra=2):
    """One hop over an implicit graph (chunk = node, rare shared term = edge)
    from the chunks BM25 selected. Adds up to `max_extra` chunks that share a
    name or title with a selected chunk but not with the question.
    Fallback when no document has an entity graph; no LLM call needed.
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


def expand_by_entity_graph(question, selected, chunks, entities, relations, max_extra=2):
    """One hop over the LLM-built entity graph (app/graph.py).

    Entities named in the question or in the selected chunks are "active";
    a relation also activates the entity at its other end. Unselected chunks
    mentioning the most active entities are added (up to `max_extra`). This
    links chunk A ("X, hướng dẫn: Y") to chunk B (only about Y) even when
    they share no other word.

    `entities`/`relations`: the selected documents' graphs, already merged.
    """
    if max_extra <= 0 or len(selected) >= len(chunks) or not entities:
        return selected

    folded_names = {name: _fold(name) for name in entities if name and name.strip()}
    if not folded_names:
        return selected

    # Which chunks mention which entity (case/diacritic-insensitive substring).
    folded_chunk_text = {_chunk_key(c): _fold(c["text"]) for c in chunks}
    mentions = {}
    for name, needle in folded_names.items():
        hits = {key for key, text in folded_chunk_text.items() if needle in text}
        if hits:
            mentions[name] = hits

    # A relation links both ends: activating one side activates the other.
    linked = {name: set() for name in folded_names}
    for rel in relations or ():
        subject, obj = (rel or {}).get("subject"), (rel or {}).get("object")
        if subject in folded_names and obj in folded_names:
            linked[subject].add(obj)
            linked[obj].add(subject)

    folded_question = _fold(question)
    active = {name for name, needle in folded_names.items() if needle in folded_question}
    for chunk in selected:
        text = folded_chunk_text.get(_chunk_key(chunk), "")
        active |= {name for name, needle in folded_names.items() if needle in text}
    active |= {linked_name for name in list(active) for linked_name in linked.get(name, ())}
    if not active:
        return selected

    selected_keys = {_chunk_key(c) for c in selected}
    scores = Counter()
    for name in active:
        for key in mentions.get(name, ()):
            if key not in selected_keys:
                scores[key] += 1
    if not scores:
        return selected

    by_key = {_chunk_key(c): c for c in chunks}
    ranked_keys = sorted(scores, key=lambda key: scores[key], reverse=True)
    return selected + [by_key[key] for key in ranked_keys[:max_extra]]


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
