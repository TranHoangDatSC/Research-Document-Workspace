from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import rag as service

router = APIRouter(tags=["rag"])


class AskRequest(BaseModel):
    question: str = Field(max_length=2000)
    model: str | None = Field(default=None, max_length=100)
    document_ids: list[str] | None = None


@router.post("/projects/{project_id}/ask")
def ask_project(project_id: UUID, payload: AskRequest):
    return service.ask_project(project_id, payload.question, payload.model, payload.document_ids)
