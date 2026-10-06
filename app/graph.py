"""Entity/relation graph of a document: one LLM call that turns extracted
text into named entities and (subject, relation, object) triples.

Run only from the "Tạo đồ thị thực thể" button (it costs API quota). Stored
in MongoDB next to `extracted_text`; used by rag.expand_by_entity_graph and,
merged per project, by the graph page.

Best-effort: on any LLM or parse failure the document simply has no graph
and retrieval falls back to rag.expand_by_shared_terms.
"""
import json
import logging
import re

from app import llm

log = logging.getLogger("uvicorn.error")

# The start of a document is enough to find its entities; keeps the call cheap.
MAX_INPUT_CHARACTERS = 20_000
MAX_ENTITIES = 40
MAX_RELATIONS = 40
MAX_NAME_LENGTH = 200
# Data extraction, not prose: deterministic.
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
    """One graph from many: entities deduplicated by exact name (so a name in
    two documents becomes one node), relations by exact triple. First-seen
    order is kept."""
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
