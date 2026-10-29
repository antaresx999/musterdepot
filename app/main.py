"""Musterdepot – simulierter Handel mit Aktien, Fonds, ETFs und Kryptowährungen."""
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, worker
from .routes import router
from .security import hash_password
from .web import BASE_DIR, Redirect, render

__version__ = "0.1.0"

log = logging.getLogger("musterdepot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

config.ensure_dirs()
db.init()


def startup():
    with db.get() as conn:
        if not conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]:
            pw = config.ADMIN_PASSWORD or secrets.token_urlsafe(12)
            cur = conn.execute("INSERT OR IGNORE INTO users (username, display_name, password_hash, role) "
                               "VALUES (?,?,?,'admin')", (config.ADMIN_USER, "Administrator", hash_password(pw)))
            if cur.rowcount and not config.ADMIN_PASSWORD:
                path = config.DATA_DIR / "initial-admin-password.txt"
                path.write_text("Benutzer: {}\nPasswort: {}\n".format(config.ADMIN_USER, pw))
                path.chmod(0o600)
                log.warning("Admin-Konto '%s' angelegt. Passwort steht in %s", config.ADMIN_USER, path)
    if config.WORKER:
        worker.start()


@asynccontextmanager
async def lifespan(app):
    startup()
    yield
    worker.stop()


app = FastAPI(title="Musterdepot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key(),
    session_cookie="musterdepot_session",
    max_age=config.SESSION_DAYS * 86400,
    same_site="lax",
    https_only=config.COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(router)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.exception_handler(Redirect)
async def _redirect_handler(request, exc):
    return RedirectResponse(exc.url, status_code=303)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", {"code": exc.status_code, "message": exc.detail}, status=exc.status_code)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}
