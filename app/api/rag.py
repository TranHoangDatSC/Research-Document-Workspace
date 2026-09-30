from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.services import documents as documents_service
from app.services import rag as service

router = APIRouter(tags=["rag"])


class AskRequest(BaseModel):
    question: str = Field(max_length=2000)
    model: str | None = Field(default=None, max_length=100)
    document_ids: list[str] | None = None


def _user_id(request):
    # Set by the require_login middleware (app/main.py).
    user = getattr(request.state, "user", None)
    return user["user_id"] if user else None


@router.post("/projects/{project_id}/ask")
def ask_project(project_id: UUID, payload: AskRequest, request: Request):
    return service.ask_project(
        project_id, payload.question, payload.model, payload.document_ids, user_id=_user_id(request),
    )


@router.get("/projects/{project_id}/chat")
def get_chat(project_id: UUID, request: Request):
    documents_service.require_project(project_id)
    return {"messages": service.get_history(project_id, _user_id(request))}


@router.delete("/projects/{project_id}/chat", status_code=204)
def clear_chat(project_id: UUID, request: Request):
    service.clear_history(project_id, _user_id(request))
