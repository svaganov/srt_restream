"""SRT Restreamer Web Application"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from cryptography.fernet import Fernet
from pathlib import Path
import os

# Base directory is the project root (parent of backend/)
BASE_DIR = Path(__file__).resolve().parent.parent

from models import init_db, get_db, User, InputStream, OutputStream
from auth import validate_csrf_and_origin
from stream_manager import init_stream_manager, shutdown_stream_manager
from api import router


def _require_secrets() -> None:
    """Ensure required secrets are configured and not fallback values."""
    secret_key = os.getenv("SECRET_KEY", "")
    secrets_key = os.getenv("SECRETS_KEY", "")
    fallback_keys = {
        "srt-restreamer-secret-key-change-in-production",
        "your-super-secret-key-change-in-production",
        "change-me-in-production-12345",
        "",
    }
    if secret_key in fallback_keys:
        raise RuntimeError(
            "SECRET_KEY is not set or uses a known fallback value. "
            "Set a strong SECRET_KEY environment variable."
        )
    if not secrets_key:
        raise RuntimeError(
            "SECRETS_KEY is not set. Generate one with:\n"
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        Fernet(secrets_key)
    except Exception as exc:
        raise RuntimeError(f"SECRETS_KEY is not a valid Fernet key: {exc}")


def _require_admin_user() -> None:
    """Ensure at least one admin user exists; otherwise bootstrap is required."""
    db: Session = next(get_db())
    try:
        if not db.query(User).first():
            raise RuntimeError(
                "No admin user found. Create one with:\n"
                "python -m backend.bootstrap_admin"
            )
    finally:
        db.close()


def _run_migrations() -> None:
    """Apply Alembic migrations to the database at startup."""
    from alembic.config import Config as AlembicConfig
    from alembic import command

    backend_dir = BASE_DIR / "backend"
    alembic_cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    # script_location in alembic.ini is relative; make it absolute so the
    # working directory does not matter.
    alembic_cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    command.upgrade(alembic_cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _run_migrations()
    _require_secrets()
    _require_admin_user()
    manager = init_stream_manager()
    _reconcile_desired_streams(manager)
    try:
        yield
    finally:
        shutdown_stream_manager()


def _reconcile_desired_streams(manager) -> None:
    """Restore desired streams after a process restart.

    Inputs are started first, then their outputs. desired_state in the DB is
    the source of truth; runtime failures surface via REST/WebSocket status.
    """
    db: Session = next(get_db())
    try:
        for inp in db.query(InputStream).filter(InputStream.desired_active.is_(True)).all():
            if manager.start_input(inp.id, inp.srt_url, passphrase_encrypted=inp.passphrase_encrypted or None):
                inp.is_active = True
            for out in inp.outputs:
                if out.desired_active:
                    if manager.start_output(inp.id, out.id, out.srt_url, passphrase_encrypted=out.passphrase_encrypted or None):
                        out.is_active = True
        db.commit()
    finally:
        db.close()


app = FastAPI(title="SRT Restreamer", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    # Enforce CSRF/Origin for state-changing requests before handling the request.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        db: Session = next(get_db())
        try:
            try:
                validate_csrf_and_origin(request, db)
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=getattr(exc, "headers", None) or {},
                )
        finally:
            db.close()
    response = await call_next(request)
    # Basic CSP for the single-page UI.
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        ),
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


# Include API routes
app.include_router(router, prefix="/api")

# Static files
app.mount("/static", StaticFiles(directory=BASE_DIR / "frontend" / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def read_root():
    with open(BASE_DIR / "frontend" / "templates" / "index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/login", response_class=HTMLResponse)
def login_page():
    with open(BASE_DIR / "frontend" / "templates" / "login.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready(db: Session = Depends(get_db)):
    # Lightweight DB check
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("UVICORN_HOST", "0.0.0.0")
    port = int(os.getenv("UVICORN_PORT", "8080"))
    # Security model relies on a single process owning process groups.
    uvicorn.run(app, host=host, port=port, workers=1)
