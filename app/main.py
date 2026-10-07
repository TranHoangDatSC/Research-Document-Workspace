"""FastAPI entry point: routers, error pages and three HTTP middlewares.

Starlette runs the middleware registered last as the outermost, so a request
passes security_headers -> require_login -> same_origin_forms -> route.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from app import access, auth, settings
from app.branding import APP_NAME
from app.services import auth as auth_service
from app.api.projects import router as projects_router
from app.api.health import router as health_router
from app.api.documents import router as documents_router
from app.api.rag import router as rag_router
from app.ui.routes import router as ui_router, error_page, is_https
from app.ui.admin import router as admin_router

@asynccontextmanager
async def lifespan(app):
    settings.apply_saved_overrides()
    settings.start_listener()
    logging.getLogger("uvicorn.error").info("application_started")
    yield
    settings.stop_listener()

app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.include_router(projects_router)
app.include_router(health_router)
app.include_router(documents_router)
app.include_router(rag_router)
app.include_router(ui_router)
app.include_router(admin_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

HTML_PREFIXES = ("/ui/", "/account", "/admin")

def is_ui(request):
    """Browser pages: errors render as HTML, a missing login redirects."""
    path = request.url.path
    return path == "/" or path.startswith(HTML_PREFIXES)

# Public account pages (no login). /docs, /redoc and /openapi.json are NOT
# public: the API map is for signed-in users, not for anyone on the internet.
ACCOUNT_FORMS = {"/login", "/signup", "/forgot-password", "/reset-password", "/verify-email", "/verify-email/resend"}
PUBLIC_PATHS = {*ACCOUNT_FORMS, "/health/live", "/health/ready"}

def is_public(request):
    path = request.url.path
    return path.startswith("/static/") or path in PUBLIC_PATHS

@app.exception_handler(HTTPException)
async def html_http_error(request: Request, exc):
    if is_ui(request):
        return error_page(request, exc.status_code, str(exc.detail))
    return await http_exception_handler(request, exc)

@app.exception_handler(RequestValidationError)
async def html_validation_error(request: Request, exc):
    if is_ui(request):
        return error_page(request, 422, "Dữ liệu không hợp lệ. Kiểm tra ID, các trường và file đã chọn.")
    return await request_validation_exception_handler(request, exc)

def same_site_origin(request):
    """CSRF check: a form post must come from this site. Host only, since
    behind Caddy the browser uses https while uvicorn sees http."""
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    if not origin:
        return True  # old browsers / non-browser clients; SameSite=Lax cookies still apply
    host = urlsplit(origin).netloc
    allowed = {request.headers.get("host", ""), urlsplit(os.environ.get("APP_BASE_URL", "")).netloc}
    return host in allowed

@app.middleware("http")
async def same_origin_forms(request: Request, call_next):
    # Account forms too: a cross-site POST to /login could sign a victim into an attacker's account.
    if request.method == "POST" and (request.url.path.startswith(HTML_PREFIXES) or request.url.path in ACCOUNT_FORMS):
        if not same_site_origin(request):
            return error_page(request, 403, "Biểu mẫu phải được gửi từ trang ứng dụng này.")
    return await call_next(request)

@app.middleware("http")
async def require_login(request: Request, call_next):
    if is_public(request):
        return await call_next(request)
    session = auth.verify_session_token(request.cookies.get(auth.SESSION_COOKIE))
    user = None
    if session is not None:
        # Checked against the database on every request, so a lock, password
        # change or "log out everywhere" takes effect at once.
        try:
            user = await run_in_threadpool(auth_service.session_user, session)
        except HTTPException as exc:
            if is_ui(request):
                return error_page(request, exc.status_code, str(exc.detail))
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if user is None:
        response = RedirectResponse("/login", status_code=303) if is_ui(request) else JSONResponse({"detail": "Not authenticated"}, status_code=401)
        if session is not None:
            response.delete_cookie(auth.SESSION_COOKIE)  # revoked: don't keep sending it
        return response
    request.state.user = user
    token = access.bind(user)
    try:
        return await call_next(request)
    finally:
        access.unbind(token)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Outermost: applies to every response, error pages included."""
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    # Reset/verification tokens travel in URLs: never leak them to other sites.
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if "content-security-policy" not in headers:
        headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    if is_https(request):
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response
