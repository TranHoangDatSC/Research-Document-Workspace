"""Server-rendered UI. Calls shared Python services, never loopback HTTP."""
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request, Form, File, UploadFile, Query, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from app.api.health import health_ready
from app.schemas.projects import ProjectCreate
from app.services import projects, documents
from app.ui.icons import icon, filesize, fmt_datetime, pretty_json

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
templates.env.globals["icon"] = icon
templates.env.filters["filesize"] = filesize
templates.env.filters["dt"] = fmt_datetime
templates.env.filters["pretty_json"] = pretty_json

# Fields stored in PostgreSQL; everything else in a merged document comes from MongoDB.
SQL_FIELDS = {"id", "project_id", "original_name", "object_name", "content_type", "size_bytes", "status", "created_at"}

def sidebar_projects():
    # Navigation must never break a page: on any storage error show an empty list.
    try:
        return projects.list_projects(50, 0)
    except Exception:
        return []

def render(request, name, status_code=200, **context):
    context.setdefault("sidebar_projects", sidebar_projects())
    return templates.TemplateResponse(request=request, name=name, context=context, status_code=status_code)

def error_page(request, status, message):
    return render(request, "error.html", status, status=status, message=message)

@router.get("/")
def home(request: Request, offset: int = Query(default=0, ge=0)):
    health = json.loads(health_ready().body)
    return render(request, "index.html", projects=projects.list_projects(20, offset), health=health, offset=offset)

@router.post("/ui/projects")
def create_project(request: Request, name: Annotated[str, Form(max_length=200)] = "", description: Annotated[str, Form(max_length=5000)] = ""):
    try:
        payload = ProjectCreate(name=name, description=description)
    except ValidationError:
        return error_page(request, 422, "Tên dự án phải có 1–200 ký tự; mô tả tối đa 5000 ký tự.")
    row = projects.create_project(payload)
    return RedirectResponse(f"/ui/projects/{row['id']}", status_code=303)

@router.get("/ui/projects/{project_id}")
def project_page(request: Request, project_id: UUID, offset: int = Query(default=0, ge=0)):
    project = projects.get_project(project_id)
    return render(request, "project_detail.html", project=project, active_project_id=project["id"], documents=documents.list_documents(project_id, 20, offset), offset=offset)

@router.post("/ui/projects/{project_id}/documents")
def upload(request: Request, project_id: UUID, file: Annotated[UploadFile, File()], tags: Annotated[str, Form(max_length=5000)] = "", authors: Annotated[str, Form(max_length=5000)] = "", custom_metadata: Annotated[str, Form(max_length=16000)] = "{}"):
    row = documents.upload_document(project_id, file, tags, authors, custom_metadata)
    return RedirectResponse(f"/ui/documents/{row['id']}", status_code=303)

@router.get("/ui/documents/{document_id}")
def document_page(request: Request, document_id: UUID):
    row = documents.get_document(document_id)
    project = projects.get_project(row["project_id"])
    mongo_document = {k: v for k, v in row.items() if k not in SQL_FIELDS}
    return render(request, "document_detail.html", document=row, project=project, active_project_id=project["id"], mongo_document=mongo_document)

@router.post("/ui/documents/{document_id}/extract")
def extract(request: Request, document_id: UUID):
    documents.extract_document(document_id)
    return RedirectResponse(f"/ui/documents/{document_id}", status_code=303)

@router.get("/ui/documents/{document_id}/delete")
def confirm_delete(request: Request, document_id: UUID):
    row = documents.document_row(document_id)
    return render(request, "delete.html", document=row, active_project_id=row["project_id"])

@router.post("/ui/documents/{document_id}/delete")
def delete(request: Request, document_id: UUID, confirm: Annotated[str, Form()] = ""):
    if confirm != "delete":
        raise HTTPException(422, "Cần xác nhận xóa tài liệu.")
    # Preserve redirect target from SQL even if metadata/object is missing.
    row = documents.document_row(document_id)
    documents.delete_document(document_id)
    return RedirectResponse(f"/ui/projects/{row['project_id']}", status_code=303)
