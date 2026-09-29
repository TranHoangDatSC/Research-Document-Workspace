from uuid import UUID
from fastapi import APIRouter, Query
from app.schemas.projects import ProjectCreate, ProjectRead
from app.services import projects as service

router = APIRouter(prefix="/projects", tags=["projects"])

@router.post("", response_model=ProjectRead, status_code=201)
def create_project(payload: ProjectCreate):
    return service.create_project(payload)

@router.get("", response_model=list[ProjectRead])
def list_projects(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    return service.list_projects(limit, offset)

@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: UUID):
    return service.get_project(project_id)

@router.put("/{project_id}", response_model=ProjectRead)
def update_project(project_id: UUID, payload: ProjectCreate):
    return service.update_project(project_id, payload)

@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: UUID):
    service.delete_project(project_id)
