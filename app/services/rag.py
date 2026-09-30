"""Ask-your-documents: full text or BM25 retrieval over extracted text + one
LLM call. No vector DB, no embeddings (see app/rag.py, app/llm.py). Persona,
rules and tuning come from the active domain (app/domains/, APP_DOMAIN).
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app import domains, llm, rag
from app.repositories import documents as repository
from app.services import documents as documents_service

log = logging.getLogger("uvicorn.error")
MAX_QUESTION_LENGTH = 2000
# Vietnam has no DST; a fixed offset avoids needing tzdata on Windows/slim images.
_LOCAL_TZ = timezone(timedelta(hours=7))


def _system_instruction(domain):
    # The model has no clock: without this, "recent", "this year" or "how old
    # is this study" are answered against its training cutoff.
    today = datetime.now(_LOCAL_TZ)
    return f"{domain.system}\n\nHôm nay là ngày {today:%d/%m/%Y}."


def _select_chunks(question, chunks, total_characters, domain):
    """(numbered chunks for the prompt, full_text flag)."""
    if total_characters <= domain.full_text_max_chars:
        return chunks, True
    ranked = rag.rank_chunks(question, chunks, domain.top_k, domain.min_relative_score)
    return rag.order_for_reading(ranked), False


def ask_project(project_id, question, model=None, document_ids=None):
    documents_service.require_project(project_id)

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
    except Exception as exc:
        log.warning("rag_storage_failed stage=read-documents error=%s", type(exc).__name__)
        raise HTTPException(503, "Document storage is temporarily unavailable") from None

    domain = domains.current()
    chunks = rag.build_chunks(documents, domain.chunk_characters, domain.chunk_overlap)
    if not chunks:
        raise HTTPException(
            409,
            'Chưa có tài liệu nào trong project được trích xuất văn bản. '
            'Vào từng tài liệu và bấm "Trích xuất văn bản" trước khi hỏi.',
        )

    total_characters = sum(len((d.get("extracted_text") or {}).get("text") or "") for d in documents)
    numbered, full_text = _select_chunks(question, chunks, total_characters, domain)
    prompt = rag.build_prompt(question, numbered, full_text=full_text)

    try:
        answer, model_used = llm.ask(
            prompt, preferred_model=model or None,
            system=_system_instruction(domain), temperature=domain.temperature,
        )
    except llm.LLMError as exc:
        log.warning("rag_llm_failed error=%s", exc.message)
        raise HTTPException(503, exc.message) from None

    cited = rag.cited_chunks(answer, numbered)
    log.info(
        "project_asked project_id=%s domain=%s mode=%s chunks_sent=%s chunks_cited=%s model=%s",
        project_id, domain.name, "full_text" if full_text else "bm25", len(numbered), len(cited), model_used,
    )
    return {
        "answer": answer,
        "model": model_used,
        # Only passages the answer cites: a list of everything sent (possibly
        # the whole document in full-text mode) says nothing about the answer.
        "sources": [
            {"ref": number, "document_id": c["document_id"], "original_name": c["original_name"], "chunk_index": c["chunk_index"]}
            for number, c in cited
        ],
    }
