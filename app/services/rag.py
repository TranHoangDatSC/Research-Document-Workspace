"""Ask-your-documents: lexical retrieval over extracted text + one LLM call.
No vector DB, no embeddings — a deliberately small RAG for a handful of
short research documents per project (see app/rag.py, app/llm.py).
"""
import logging

from fastapi import HTTPException

from app import llm, rag
from app.repositories import documents as repository
from app.services import documents as documents_service

log = logging.getLogger("uvicorn.error")
MAX_QUESTION_LENGTH = 2000


def ask_project(project_id, question, model=None):
    documents_service.require_project(project_id)

    question = (question or "").strip()
    if not question:
        raise HTTPException(422, "Câu hỏi không được để trống")
    if len(question) > MAX_QUESTION_LENGTH:
        raise HTTPException(422, f"Câu hỏi tối đa {MAX_QUESTION_LENGTH} ký tự")

    try:
        rows = repository.list_documents(project_id, 200, 0)
        documents = []
        for row in rows:
            if row["status"] != "ready":
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

    chunks = rag.build_chunks(documents)
    if not chunks:
        raise HTTPException(
            409,
            'Chưa có tài liệu nào trong project được trích xuất văn bản. '
            'Vào từng tài liệu và bấm "Trích xuất văn bản" trước khi hỏi.',
        )

    ranked = rag.rank_chunks(question, chunks)
    prompt = rag.build_prompt(question, ranked)

    try:
        answer, model_used = llm.ask(prompt, preferred_model=model or None)
    except llm.LLMError as exc:
        log.warning("rag_llm_failed error=%s", exc.message)
        raise HTTPException(503, exc.message) from None

    log.info("project_asked project_id=%s chunks_used=%s model=%s", project_id, len(ranked), model_used)
    return {
        "answer": answer,
        "model": model_used,
        "sources": [
            {"document_id": c["document_id"], "original_name": c["original_name"], "chunk_index": c["chunk_index"]}
            for c in ranked
        ],
    }
