"""Server-rendered UI. Calls shared Python services, never loopback HTTP."""
import json
import logging
import os
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Request, Form, File, UploadFile, Query, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from app import auth as core_auth, file_types, llm, mailer, ratelimit
from app.api.health import health_ready
from app.schemas.projects import ProjectCreate
from app.services import auth as auth_service, projects, documents, rag as rag_service
from app.ui.icons import icon, filesize, fmt_datetime, pretty_json

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
templates.env.globals["icon"] = icon
templates.env.filters["filesize"] = filesize
templates.env.filters["dt"] = fmt_datetime
templates.env.filters["pretty_json"] = pretty_json
# File kinds for icons, previews and the upload picker (app/file_types.py).
templates.env.filters["file_kind"] = lambda doc: file_types.kind_of(doc["object_name"]) or file_types.KINDS["document"]
templates.env.globals["upload_accept"] = file_types.accept_attribute
templates.env.globals["upload_limits_json"] = lambda: json.dumps(file_types.limits_by_extension())
templates.env.globals["upload_summary"] = file_types.summary
# Cache-busts /static/* on every process start so a redeploy can't get stuck
# behind a browser's cached style.css/app.js.
templates.env.globals["asset_version"] = str(int(time.time()))

# Fields stored in PostgreSQL; everything else in a merged document comes from MongoDB.
SQL_FIELDS = {"id", "project_id", "original_name", "object_name", "content_type", "size_bytes", "status", "created_at"}

SIDEBAR_RECENT_PROJECTS = 3

def sidebar_projects():
    # Navigation must never break a page: on any storage error show an empty list.
    try:
        return projects.list_projects(SIDEBAR_RECENT_PROJECTS, 0)
    except Exception:
        return []

def render(request, name, status_code=200, **context):
    context.setdefault("sidebar_projects", sidebar_projects())
    context.setdefault("current_user", getattr(request.state, "user", None))
    return templates.TemplateResponse(request=request, name=name, context=context, status_code=status_code)

def error_page(request, status, message):
    return render(request, "error.html", status, status=status, message=message)

# ----- account pages (public: no login needed) -----

def auth_page(request, name, status_code=200, headers=None, **context):
    """Login / sign-up / password pages: errors are shown on the same form
    (with what was typed), never as the generic error page."""
    context.setdefault("error", "")
    context.setdefault("notice", "")
    context.setdefault("signup_enabled", auth_service.signup_enabled())
    return templates.TemplateResponse(request=request, name=name, context=context, status_code=status_code, headers=headers)

def is_https(request):
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"

def start_session(request, user, to="/"):
    response = RedirectResponse(to, status_code=303)
    response.set_cookie(
        core_auth.SESSION_COOKIE, auth_service.session_token(user), max_age=core_auth.SESSION_MAX_AGE_SECONDS,
        httponly=True, samesite="lax", secure=is_https(request),  # never sent over plain http in production
    )
    return response

def email_link_base(request):
    """Base URL for emailed links. Must come from APP_BASE_URL when mail is
    really sent: building it from the request's Host header would let anyone
    trigger an email whose link points at their own site. None = don't send."""
    configured = os.environ.get("APP_BASE_URL", "").strip()
    if configured:
        return configured
    if mailer.configured():
        logging.getLogger("uvicorn.error").error("email_link_skipped reason=APP_BASE_URL-not-set-while-SMTP-configured")
        return None
    return str(request.base_url)

LOGIN_NOTICES = {
    "reset": "Đã đổi mật khẩu. Đăng nhập bằng mật khẩu mới.",
    "verified": "Đã xác minh email. Bạn có thể đăng nhập.",
    "signed-out": "Đã đăng xuất khỏi mọi thiết bị.",
}

@router.get("/login")
def login_page(request: Request, done: str = ""):
    return auth_page(request, "login.html", notice=LOGIN_NOTICES.get(done, ""))

@router.post("/login")
def login(request: Request, username: Annotated[str, Form(max_length=50)] = "", password: Annotated[str, Form(max_length=200)] = ""):
    try:
        ratelimit.check("login", request)
    except HTTPException as exc:
        return auth_page(request, "login.html", exc.status_code, exc.headers, error=exc.detail, username=username)
    user = auth_service.authenticate(username.strip(), password)
    if user is None:
        return auth_page(request, "login.html", 401, error="Sai username hoặc mật khẩu, hoặc tài khoản đã bị khóa.", username=username)
    if not user["email_verified"]:
        # Only said after the right password, so it reveals nothing new.
        return auth_page(request, "login.html", 403, username=username, unverified_email=user["email"],
                         error="Tài khoản chưa xác minh email. Mở liên kết trong email đăng ký, hoặc gửi lại email xác minh.")
    return start_session(request, user)

@router.get("/signup")
def signup_page(request: Request):
    if not auth_service.signup_enabled():
        return auth_page(request, "signup.html", 403, error="Đăng ký tài khoản đang đóng. Liên hệ quản trị viên để được cấp tài khoản.")
    return auth_page(request, "signup.html")

@router.post("/signup")
def signup(request: Request, username: Annotated[str, Form(max_length=50)] = "", email: Annotated[str, Form(max_length=254)] = "", password: Annotated[str, Form(max_length=200)] = "", password_confirm: Annotated[str, Form(max_length=200)] = ""):
    try:
        ratelimit.check("signup", request)
        user = auth_service.register(username.strip(), email, password, password_confirm, email_link_base(request))
    except HTTPException as exc:
        return auth_page(request, "signup.html", exc.status_code, exc.headers, error=exc.detail, username=username, email=email)
    # No session yet: the account works once the emailed link is opened.
    return auth_page(request, "signup.html", 201, sent=True, email=user["email"])

@router.get("/verify-email")
def verify_email(request: Request, token: str = Query(default="", max_length=200)):
    if auth_service.verify_email(token):
        return RedirectResponse("/login?done=verified", status_code=303)
    return auth_page(request, "verify_email.html", 400, invalid=True)

@router.get("/verify-email/resend")
def resend_verification_page(request: Request, email: str = Query(default="", max_length=254)):
    return auth_page(request, "verify_email.html", email=email)

@router.post("/verify-email/resend")
def resend_verification(request: Request, email: Annotated[str, Form(max_length=254)] = ""):
    try:
        ratelimit.check("verify-resend", request)
    except HTTPException as exc:
        return auth_page(request, "verify_email.html", exc.status_code, exc.headers, error=exc.detail, email=email)
    auth_service.resend_verification(email, email_link_base(request))
    return auth_page(request, "verify_email.html", sent=True, email=email)

@router.get("/forgot-password")
def forgot_password_page(request: Request):
    return auth_page(request, "forgot_password.html")

@router.post("/forgot-password")
def forgot_password(request: Request, email: Annotated[str, Form(max_length=254)] = ""):
    try:
        ratelimit.check("forgot-password", request)
    except HTTPException as exc:
        return auth_page(request, "forgot_password.html", exc.status_code, exc.headers, error=exc.detail, email=email)
    base = email_link_base(request)
    if base is not None:
        auth_service.request_password_reset(email, base)
    # Same answer whether or not the email has an account.
    return auth_page(request, "forgot_password.html", sent=True, email=email)

@router.get("/reset-password")
def reset_password_page(request: Request, token: str = Query(default="", max_length=200)):
    user = auth_service.reset_token_user(token)
    return auth_page(request, "reset_password.html", token=token, valid=user is not None, username=user["username"] if user else "")

@router.post("/reset-password")
def reset_password(request: Request, token: Annotated[str, Form(max_length=200)] = "", password: Annotated[str, Form(max_length=200)] = "", password_confirm: Annotated[str, Form(max_length=200)] = ""):
    try:
        ratelimit.check("reset-password", request)
        auth_service.reset_password(token, password, password_confirm)
    except HTTPException as exc:
        return auth_page(request, "reset_password.html", exc.status_code, exc.headers, error=exc.detail, token=token, valid=exc.status_code != 400)
    return RedirectResponse("/login?done=reset", status_code=303)

# ----- signed-in account page -----

@router.get("/account")
def account_page(request: Request, changed: str = ""):
    account = auth_service.get_account(current_user_id(request))
    notice = "Đã đổi mật khẩu. Các thiết bị khác đã bị đăng xuất." if changed else ""
    return render(request, "account.html", account=account, notice=notice, error="")

@router.post("/account/password")
def change_password(request: Request, current_password: Annotated[str, Form(max_length=200)] = "", password: Annotated[str, Form(max_length=200)] = "", password_confirm: Annotated[str, Form(max_length=200)] = ""):
    try:
        user = auth_service.change_password(current_user_id(request), current_password, password, password_confirm)
    except HTTPException as exc:
        account = auth_service.get_account(current_user_id(request))
        return render(request, "account.html", exc.status_code, account=account, notice="", error=exc.detail)
    # Every session was revoked by the change; keep this device signed in.
    return start_session(request, user, "/account?changed=1")

@router.post("/account/logout-everywhere")
def logout_everywhere(request: Request):
    auth_service.logout_everywhere(current_user_id(request))
    response = RedirectResponse("/login?done=signed-out", status_code=303)
    response.delete_cookie(core_auth.SESSION_COOKIE)
    return response

@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(core_auth.SESSION_COOKIE)
    return response

PROJECTS_PAGE_SIZE = 6

def pagination_window(page, total_pages):
    """Page numbers to render, with None standing in for an ellipsis gap."""
    pages = sorted({1, total_pages, page - 1, page, page + 1} & set(range(1, total_pages + 1)))
    windowed = []
    for i, n in enumerate(pages):
        if i and n - pages[i - 1] > 1:
            windowed.append(None)
        windowed.append(n)
    return windowed

@router.get("/")
def home(request: Request, page: int = Query(default=1, ge=1), q: str = Query(default="", max_length=200)):
    health = json.loads(health_ready().body)
    query = q.strip() or None
    total = projects.count_projects(query)
    total_pages = max(1, -(-total // PROJECTS_PAGE_SIZE))
    page = min(page, total_pages)
    offset = (page - 1) * PROJECTS_PAGE_SIZE
    return render(
        request, "index.html", health=health,
        projects=projects.list_projects(PROJECTS_PAGE_SIZE, offset, query),
        page=page, total_pages=total_pages, total_projects=total, search_query=q.strip(),
        page_numbers=pagination_window(page, total_pages),
    )

@router.post("/ui/projects")
def create_project(request: Request, name: Annotated[str, Form(max_length=200)] = "", description: Annotated[str, Form(max_length=5000)] = ""):
    try:
        payload = ProjectCreate(name=name, description=description)
    except ValidationError:
        return error_page(request, 422, "Tên dự án phải có 1–200 ký tự; mô tả tối đa 5000 ký tự.")
    row = projects.create_project(payload)
    return RedirectResponse(f"/ui/projects/{row['id']}", status_code=303)

SOURCES_PAGE_SIZE = 20

def sources_context(project_id, offset, q, kind):
    """Template context for the sources sidebar. The search/filter/page live in
    the query string and are carried on every sidebar link (`source_qs`), so
    the sidebar stays as it was while documents open in the main panel."""
    q = q.strip()
    kind = kind if kind in file_types.KINDS else ""
    params = {k: v for k, v in (("q", q), ("kind", kind), ("offset", offset)) if v}
    return {
        "documents": documents.list_documents(project_id, SOURCES_PAGE_SIZE, offset, q or None, kind or None),
        "offset": offset,
        "source_q": q,
        "source_kind": kind,
        "source_kinds": list(file_types.KINDS.values()),
        "source_qs": urlencode(params),
        "source_filter_qs": urlencode({k: v for k, v in params.items() if k != "offset"}),
    }

@router.get("/ui/projects/{project_id}")
def project_page(request: Request, project_id: UUID, offset: int = Query(default=0, ge=0), q: str = Query(default="", max_length=200), kind: str = Query(default="", max_length=20)):
    project = projects.get_project(project_id)
    return render(
        request, "project_detail.html", project=project, active_project_id=project["id"],
        kind_totals=documents.kind_totals(project_id), ai_ready=documents.ai_ready_count(project_id), rag_models=llm.available_models(),
        chat_messages=chat_history(request, project_id), **sources_context(project_id, offset, q, kind),
    )

@router.get("/ui/projects/{project_id}/edit")
def edit_project_page(request: Request, project_id: UUID):
    project = projects.get_project(project_id)
    return render(request, "edit_project.html", project=project, active_project_id=project["id"])

@router.post("/ui/projects/{project_id}/edit")
def edit_project(request: Request, project_id: UUID, name: Annotated[str, Form(max_length=200)] = "", description: Annotated[str, Form(max_length=5000)] = ""):
    try:
        payload = ProjectCreate(name=name, description=description)
    except ValidationError:
        return error_page(request, 422, "Tên dự án phải có 1–200 ký tự; mô tả tối đa 5000 ký tự.")
    projects.update_project(project_id, payload)
    return RedirectResponse(f"/ui/projects/{project_id}", status_code=303)

@router.get("/ui/projects/{project_id}/delete")
def confirm_delete_project(request: Request, project_id: UUID):
    project = projects.get_project(project_id)
    return render(request, "delete_project.html", project=project, active_project_id=project["id"])

@router.post("/ui/projects/{project_id}/delete")
def delete_project(request: Request, project_id: UUID, confirm: Annotated[str, Form()] = ""):
    if confirm != "delete":
        raise HTTPException(422, "Cần xác nhận xóa dự án.")
    projects.delete_project(project_id)
    return RedirectResponse("/", status_code=303)

@router.post("/ui/projects/{project_id}/documents")
def upload(request: Request, project_id: UUID, file: Annotated[UploadFile, File()], tags: Annotated[str, Form(max_length=5000)] = "", authors: Annotated[str, Form(max_length=5000)] = "", custom_metadata: Annotated[str, Form(max_length=16000)] = "{}"):
    row = documents.upload_document(project_id, file, tags, authors, custom_metadata)
    return RedirectResponse(f"/ui/documents/{row['id']}", status_code=303)

def current_user_id(request):
    user = getattr(request.state, "user", None)
    return user["user_id"] if user else None

def chat_history(request, project_id):
    user_id = current_user_id(request)
    return rag_service.get_history(project_id, user_id) if user_id else []

@router.post("/ui/projects/{project_id}/ask")
def ask_project(request: Request, project_id: UUID, question: Annotated[str, Form(max_length=2000)] = "", model: Annotated[str, Form(max_length=100)] = "", document_ids: Annotated[list[str], Form()] = []):
    projects.get_project(project_id)
    rag_service.ask_project(project_id, question, model or None, document_ids or None, user_id=current_user_id(request))
    # The exchange is now in the stored history, which the project page renders.
    return RedirectResponse(f"/ui/projects/{project_id}#ai-panel", status_code=303)

@router.post("/ui/projects/{project_id}/chat/clear")
def clear_chat(request: Request, project_id: UUID):
    rag_service.clear_history(project_id, current_user_id(request))
    return RedirectResponse(f"/ui/projects/{project_id}", status_code=303)

@router.get("/ui/documents/{document_id}")
def document_page(request: Request, document_id: UUID, offset: int = Query(default=0, ge=0), q: str = Query(default="", max_length=200), kind: str = Query(default="", max_length=20)):
    row = documents.get_document(document_id)
    project = projects.get_project(row["project_id"])
    mongo_document = {k: v for k, v in row.items() if k not in SQL_FIELDS}
    return render(
        request, "document_detail.html", document=row, project=project, active_project_id=project["id"],
        mongo_document=mongo_document, active_document_id=row["id"], rag_models=llm.available_models(),
        chat_messages=chat_history(request, project["id"]), **sources_context(project["id"], offset, q, kind),
    )

@router.get("/ui/documents/{document_id}/edit")
def edit_document_page(request: Request, document_id: UUID):
    row = documents.get_document(document_id)
    project = projects.get_project(row["project_id"])
    return render(request, "edit_document.html", document=row, project=project, active_project_id=project["id"])

@router.post("/ui/documents/{document_id}/edit")
def edit_document(request: Request, document_id: UUID, tags: Annotated[str, Form(max_length=5000)] = "", authors: Annotated[str, Form(max_length=5000)] = "", custom_metadata: Annotated[str, Form(max_length=16000)] = "{}"):
    documents.update_metadata(document_id, tags, authors, custom_metadata)
    return RedirectResponse(f"/ui/documents/{document_id}", status_code=303)

@router.post("/ui/documents/{document_id}/extract")
def extract(request: Request, document_id: UUID):
    documents.extract_document(document_id)
    # Land back on the extracted-text tab, not the default "Thông tin" tab (app.js reads the hash).
    return RedirectResponse(f"/ui/documents/{document_id}#extract", status_code=303)

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
