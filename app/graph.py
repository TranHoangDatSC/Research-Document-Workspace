"""Entity/relation graph extraction: one LLM call per document, triggered by
the explicit "Tạo đồ thị thực thể" button (services.documents.extract_entity_graph)
— never automatically, since it costs API quota just like media analysis.
Turns free text into a short list of named entities and (subject, relation,
object) triples. Stored in MongoDB alongside `extracted_text` (see
repositories.documents.update_entity_graph) and read back by
rag.expand_by_entity_graph for graph-based retrieval (app/rag.py), and by
merge_graphs below for the project-wide view (services.documents.project_entity_graph).

Best-effort only, by design: a document's extracted *text* is never blocked
on this, and no caller needs to know whether it worked. If the model is
unavailable, over quota, or returns something that doesn't parse as the
expected JSON shape, the document just has no graph, and retrieval falls
back to the zero-infrastructure rag.expand_by_shared_terms instead — see
that function's docstring for why this one is a drop-in upgrade, not a
separate code path callers have to choose between.
"""
import json
import logging
import re

from app import llm

log = logging.getLogger("uvicorn.error")

# Entities rarely need the whole document to be identified, and a shorter
# prompt means a faster, cheaper call — this is one extra LLM call per
# document, so it should stay light.
MAX_INPUT_CHARACTERS = 20_000
MAX_ENTITIES = 40
MAX_RELATIONS = 40
MAX_NAME_LENGTH = 200
# Deterministic, not creative: this is data extraction, not prose.
TEMPERATURE = 0.0

_PROMPT = """Đọc đoạn văn bản tài liệu dưới đây. Liệt kê:
1. "entities": các thực thể quan trọng được nhắc tới — người, tổ chức, đề tài/dự án, địa điểm, mốc thời gian — ghi đúng tên gọi xuất hiện trong văn bản.
2. "relations": mối quan hệ giữa các thực thể đó, dạng {{"subject": tên thực thể, "relation": mô tả ngắn gọn, "object": tên thực thể}}.

CHỈ trả về một JSON object hợp lệ, không kèm giải thích, không bọc trong markdown, đúng khuôn mẫu:
{{"entities": ["...", ...], "relations": [{{"subject": "...", "relation": "...", "object": "..."}}, ...]}}

Nếu văn bản không có thực thể/quan hệ rõ ràng, trả về {{"entities": [], "relations": []}}.

VĂN BẢN:
{text}"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_code_fence(text):
    """Models asked for "just JSON" sometimes wrap it in a markdown fence anyway."""
    return _FENCE_RE.sub("", text.strip())


def _clean_name(value):
    return str(value or "").strip()[:MAX_NAME_LENGTH]


def _coerce(data):
    """Validates and caps the model's output; None if the shape is unusable."""
    if not isinstance(data, dict):
        return None
    raw_entities, raw_relations = data.get("entities"), data.get("relations")
    if not isinstance(raw_entities, list) or not isinstance(raw_relations, list):
        return None

    entities, seen = [], set()
    for item in raw_entities:
        name = _clean_name(item) if isinstance(item, str) else ""
        if name and name not in seen:
            seen.add(name)
            entities.append(name)
        if len(entities) >= MAX_ENTITIES:
            break

    relations = []
    for item in raw_relations:
        if not isinstance(item, dict):
            continue
        subject, relation, obj = _clean_name(item.get("subject")), _clean_name(item.get("relation")), _clean_name(item.get("object"))
        if subject and relation and obj:
            relations.append({"subject": subject, "relation": relation, "object": obj})
        if len(relations) >= MAX_RELATIONS:
            break

    return {"entities": entities, "relations": relations}


def extract_graph(text):
    """{"entities": [...], "relations": [...]} from a document's extracted
    text, or None — never raises; a failed or unparseable extraction just
    means no graph for this document."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        answer, _ = llm.ask(_PROMPT.format(text=text[:MAX_INPUT_CHARACTERS]), temperature=TEMPERATURE, source="graph")
    except llm.LLMError as exc:
        log.info("graph_extraction_llm_failed error=%s", exc.message)
        return None
    try:
        data = json.loads(_strip_code_fence(answer))
    except (ValueError, TypeError):
        log.info("graph_extraction_unparseable_json")
        return None
    graph = _coerce(data)
    if graph is None:
        log.info("graph_extraction_unexpected_shape")
    return graph


def merge_graphs(graphs):
    """Combines several documents' graphs into one: entities deduplicated by
    exact name (first-seen order kept — the same person/project mentioned in
    two documents becomes one node, which is the whole point of a *project*
    graph instead of one isolated graph per document), relations
    deduplicated by the exact (subject, relation, object) triple (the same
    sentence often gets re-extracted near-verbatim when it appears in more
    than one document)."""
    entities, seen_entities = [], set()
    relations, seen_relations = [], set()
    for graph in graphs:
        for name in (graph or {}).get("entities") or []:
            if name not in seen_entities:
                seen_entities.add(name)
                entities.append(name)
        for rel in (graph or {}).get("relations") or []:
            key = (rel.get("subject"), rel.get("relation"), rel.get("object"))
            if key not in seen_relations:
                seen_relations.add(key)
                relations.append(rel)
    return {"entities": entities, "relations": relations}
