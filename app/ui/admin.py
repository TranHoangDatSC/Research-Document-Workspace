"""Admin-only account management pages. Two roles today: user, admin."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import RedirectResponse

from app.services import auth as service
from app.ui.routes import render

router = APIRouter(prefix="/admin", include_in_schema=False)


def require_admin(request: Request):
    user = request.state.user
    if user["role"] != "admin":
        raise HTTPException(403, "Chỉ quản trị viên mới truy cập được trang này")
    return user


@router.get("/users")
def users_page(request: Request):
    current_user = require_admin(request)
    return render(request, "admin_users.html", users=service.list_users(), current_user=current_user)


@router.post("/users")
def create_user(
    request: Request,
    username: Annotated[str, Form(max_length=50)] = "",
    password: Annotated[str, Form(max_length=200)] = "",
    role: Annotated[str, Form()] = "user",
):
    require_admin(request)
    service.create_user(username.strip(), password, role)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/role")
def change_role(request: Request, user_id: UUID, role: Annotated[str, Form()] = "user"):
    current_user = require_admin(request)
    service.set_role(user_id, role, current_user["user_id"])
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/active")
def toggle_active(request: Request, user_id: UUID, is_active: Annotated[str, Form()] = "true"):
    current_user = require_admin(request)
    service.set_active(user_id, is_active == "true", current_user["user_id"])
    return RedirectResponse("/admin/users", status_code=303)
