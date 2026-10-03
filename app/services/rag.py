"""Ask-your-documents: full text or BM25 retrieval over extracted text + one
LLM call. No vector DB, no embeddings (see app/rag.py, app/llm.py). Persona,
rules and tuning come from the active domain (app/domains/, APP_DOMAIN).
"""
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException

from app import domains, llm, rag
from app.repositories import chats as chats_repository
from app.repositories import documents as repository
from app.services import documents as documents_service

log = logging.getLogger("uvicorn.error")
MAX_QUESTION_LENGTH = 2000
# Vietnam has no DST; a fixed offset avoids needing tzdata on Windows/slim images.
_LOCAL_TZ = timezone(timedelta(hours=7))

# Earlier turns sent to the model: enough for "explain point 2 further" or
# "compare that with the other paper", small enough that a long conversation
# doesn't crowd out the documents. Long answers are clipped — the model only
# needs the gist of what it said, the documents are resent every turn anyway.
HISTORY_MESSAGES = 10
HISTORY_MESSAGE_MAX_CHARS = 4000
# Shown in the UI when the chat panel opens.
DISPLAY_MESSAGES = 50


def _history_turns(messages):
    """Stored messages -> [(role, text)] for llm.ask. Citation numbers are
    removed from old answers: passages are renumbered every turn, so an old
    "[3]" would point the model at the wrong passage."""
    turns = []
    for m in messages:
        text = m.get("content") or ""
        if m.get("role") == "model":
            text = rag.strip_citations(text)
        if len(text) > HISTORY_MESSAGE_MAX_CHARS:
            text = text[:HISTORY_MESSAGE_MAX_CHARS] + " …"
        turns.append((m.get("role"), text))
    return turns


def _load_history(project_id, user_id, chat_id):
    if user_id is None:
        return []
    try:
        return chats_repository.list_messages(project_id, user_id, chat_id, HISTORY_MESSAGES)
    except Exception as exc:
        # History improves answers but is never required for one.
        log.warning("chat_storage_failed stage=read-history error=%s", type(exc).__name__)
        return []


def _save_exchange(project_id, user_id, chat_id, question, result):
    if user_id is None:
        return
    try:
        chats_repository.add_messages([
            {"project_id": project_id, "user_id": user_id, "chat_id": chat_id, "role": "user", "content": question},
            {
                "project_id": project_id, "user_id": user_id, "chat_id": chat_id, "role": "model",
                "content": result["answer"], "sources": result["sources"], "model": result["model"],
            },
        ])
    except Exception as exc:
        log.warning("chat_storage_failed stage=save-exchange error=%s", type(exc).__name__)


def get_history(project_id, user_id, chat_id=None):
    """(messages, resolved_chat_id) for display — never breaks the page, an
    unreadable history shows as empty. chat_id=None resolves to the most
    recently used thread; (.., None) if this (project, user) has no thread yet."""
    if user_id is None:
        return [], None
    try:
        if chat_id is None:
            threads = chats_repository.list_chats(project_id, user_id)
            if not threads:
                return [], None
            chat_id = threads[0]["chat_id"]
        return chats_repository.list_messages(project_id, user_id, chat_id, DISPLAY_MESSAGES), chat_id
    except Exception as exc:
        log.warning("chat_storage_failed stage=display-history error=%s", type(exc).__name__)
        return [], chat_id


def list_chat_threads(project_id, user_id):
    """For the chat switcher: every past thread, newest-used first. Never
    breaks the page — an unreadable list just shows as empty."""
    if user_id is None:
        return []
    try:
        return chats_repository.list_chats(project_id, user_id)
    except Exception as exc:
        log.warning("chat_storage_failed stage=list-chats error=%s", type(exc).__name__)
        return []


def delete_chat(project_id, user_id, chat_id):
    documents_service.require_project(project_id)
    try:
        deleted = chats_repository.delete_chat(project_id, user_id, chat_id)
    except Exception as exc:
        log.warning("chat_storage_failed stage=delete error=%s", type(exc).__name__)
        raise HTTPException(503, "Chat storage is temporarily unavailable") from None
    log.info("chat_deleted project_id=%s chat_id=%s messages=%s", project_id, chat_id, deleted)


def _system_instruction(domain):
    # The model has no clock: without this, "recent", "this year" or "how old
    # is this study" are answered against its training cutoff.
    today = datetime.now(_LOCAL_TZ)
    return f"{domain.system}\n\nHôm nay là ngày {today:%d/%m/%Y}."


def _select_chunks(question, chunks, total_characters, domain, history, entities, relations):
    """(numbered chunks for the prompt, full_text flag, {(document_id, chunk_index)}
    of chunks added by graph expansion — used to mark those sources in the
    answer as "found via the graph" instead of a direct keyword match)."""
    if total_characters <= domain.full_text_max_chars:
        return chunks, True, set()
    # A follow-up ("explain that further") has almost no searchable words of
    # its own; the previous question carries the topic.
    previous = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "user"), "")
    query = f"{previous}\n{question}" if previous else question
    ranked = rag.rank_chunks(query, chunks, domain.top_k, domain.min_relative_score)
    # The real entity/relation graph (app/graph.py) wins whenever at least one
    # selected document has one; expand_by_shared_terms is the fallback for
    # documents extracted before the graph existed, or whose extraction failed.
    if entities:
        expanded = rag.expand_by_entity_graph(query, ranked, chunks, entities, relations, domain.graph_expansion_max_chunks)
    else:
        expanded = rag.expand_by_shared_terms(ranked, chunks, domain.graph_expansion_max_chunks)
    added = expanded[len(ranked):]
    graph_keys = {(c["document_id"], c["chunk_index"]) for c in added}
    return rag.order_for_reading(expanded), False, graph_keys


def ask_project(project_id, question, model=None, document_ids=None, user_id=None, chat_id=None):
    """`user_id` None = stateless (no history read or written). `chat_id`
    None/omitted = continue the most recently used thread (or start one if
    this (project, user) has none yet) — the old single-thread behaviour, so
    a caller that never thinks about threads at all still gets a continuous
    conversation. To start a genuinely new thread instead (a project can
    hold several — see app/repositories/chats.py), pass a chat_id nothing
    has used yet; the UI does this by generating one client-side when
    "+ Cuộc trò chuyện mới" is clicked (app/static/app.js). The resolved
    chat_id always comes back in the result, so the caller can keep sending
    it on the next question in the same thread."""
    documents_service.require_project(project_id)
    if user_id is not None and not chat_id:
        try:
            threads = chats_repository.list_chats(project_id, user_id)
        except Exception as exc:
            log.warning("chat_storage_failed stage=resolve-chat error=%s", type(exc).__name__)
            threads = []
        chat_id = threads[0]["chat_id"] if threads else str(uuid4())

    question = (question or "").strip()
    if not question:
        raise HTTPException(422, "Câu hỏi không được để trống")
    if len(question) > MAX_QUESTION_LENGTH:
        raise HTTPException(422, f"Câu hỏi tối đa {MAX_QUESTION_LENGTH} ký tự")

    # None = no filter (use every ready document); an explicit, possibly
    # empty, list scopes the answer to just the checked sources.
    selected = {str(d) for d in document_ids} if document_ids is not None else None
    if selected is not None and not selected:
        raise HTTPException(422, "Chọn ít nhất một tài liệu để hỏi.")

    try:
        rows = repository.list_documents(project_id, 200, 0)
        documents = []
        # Merged across every selected document, deduplicated: expand_by_entity_graph
        # (app/rag.py) doesn't care which document an entity or relation came
        # from, and a person/project mentioned in two documents should link them.
        entities, entity_seen, relations = [], set(), []
        for row in rows:
            if row["status"] != "ready":
                continue
            if selected is not None and str(row["id"]) not in selected:
                continue
            details = repository.get_details(row["id"])
            if details is None:
                continue
            documents.append({
                "document_id": details["document_id"],
                "original_name": row["original_name"],
                "extracted_text": details.get("extracted_text"),
            })
            graph = details.get("entity_graph") or {}
            for name in graph.get("entities") or []:
                if name not in entity_seen:
                    entity_seen.add(name)
                    entities.append(name)
            relations.extend(graph.get("relations") or [])
    except Exception as exc:
        log.warning("rag_storage_failed stage=read-documents error=%s", type(exc).__name__)
        raise HTTPException(503, "Document storage is temporarily unavailable") from None

    domain = domains.current()
    chunks = rag.build_chunks(documents, domain.chunk_characters, domain.chunk_overlap)
    # Selected but not readable yet (an image not analysed, a corrupt file):
    # told to the person instead of being silently left out of the answer.
    unread = [d["original_name"] for d in documents if not ((d.get("extracted_text") or {}).get("text") or "").strip()]
    if not chunks:
        raise HTTPException(
            409,
            "AI chưa đọc được tệp nào trong các tệp đã chọn. Mở từng tệp, tab \"Văn bản trích xuất\": "
            "bấm \"Trích xuất văn bản\", hoặc \"Phân tích bằng AI\" với ảnh, âm thanh, video.",
        )

    total_characters = sum(len((d.get("extracted_text") or {}).get("text") or "") for d in documents)
    history = _load_history(project_id, user_id, chat_id)
    numbered, full_text, graph_keys = _select_chunks(question, chunks, total_characters, domain, history, entities, relations)
    prompt = rag.build_prompt(question, numbered, full_text=full_text)

    try:
        answer, model_used = llm.ask(
            prompt, preferred_model=model or None,
            system=_system_instruction(domain), temperature=domain.temperature,
            history=_history_turns(history), source="ask",
        )
    except llm.LLMError as exc:
        log.warning("rag_llm_failed error=%s", exc.message)
        raise HTTPException(503, exc.message) from None

    cited = rag.cited_chunks(answer, numbered)
    log.info(
        "project_asked project_id=%s domain=%s mode=%s chunks_sent=%s chunks_graph_expanded=%s "
        "graph_source=%s graph_entities=%s chunks_cited=%s history=%s model=%s",
        project_id, domain.name, "full_text" if full_text else "bm25", len(numbered), len(graph_keys),
        "entity_graph" if entities else "shared_terms", len(entities), len(cited), len(history), model_used,
    )
    result = {
        "answer": answer,
        "model": model_used,
        "chat_id": chat_id,
        # Only passages the answer cites: a list of everything sent (possibly
        # the whole document in full-text mode) says nothing about the answer.
        "sources": [
            {
                "ref": number, "document_id": c["document_id"], "original_name": c["original_name"], "chunk_index": c["chunk_index"],
                # Found through a graph hop (app/rag.py), not a direct keyword
                # match on the question — surfaced in the UI so the graph's
                # contribution is actually visible, not just a silent retrieval detail.
                "via_graph": (c["document_id"], c["chunk_index"]) in graph_keys,
            }
            for number, c in cited
        ],
        "unread": unread,
    }
    _save_exchange(project_id, user_id, chat_id, question, result)
    return result
