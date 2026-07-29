"""Authentication and session management"""
import hashlib
import os
import secrets
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from models import User, UserSession, get_db

# Session lifetime: 8 hours
SESSION_LIFETIME_MINUTES = 480
MIN_PASSWORD_LENGTH = 12

# Login rate limiting
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60
_login_attempts: dict[str, deque[float]] = {}

pwd_hasher = PasswordHasher()


def check_login_rate_limit(client_ip: str) -> None:
    """Raise 429 if the client has exceeded the login attempt limit."""
    now = time.time()
    attempts = _login_attempts.setdefault(client_ip, deque())
    while attempts and attempts[0] < now - LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
        )
    attempts.append(now)


def hash_password(password: str) -> str:
    return pwd_hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        pwd_hasher.verify(hashed, password)
        return True
    except VerifyMismatchError:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_token() -> str:
    return secrets.token_hex(32)


def create_session(
    db: Session,
    user_id: int,
    lifetime_minutes: int = SESSION_LIFETIME_MINUTES,
) -> tuple[str, str]:
    """Create a new user session. Returns (session_token, csrf_token)."""
    token = _generate_token()
    csrf_token = secrets.token_hex(32)
    expires_at = datetime.utcnow() + timedelta(minutes=lifetime_minutes)
    session = UserSession(
        user_id=user_id,
        token_hash=_hash_token(token),
        csrf_token=csrf_token,
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()
    return token, csrf_token


def _get_cookie_token(request: Request) -> Optional[str]:
    return request.cookies.get("session")


def _get_valid_session(
    db: Session, token: Optional[str]
) -> Optional[UserSession]:
    if not token:
        return None
    token_hash = _hash_token(token)
    session = (
        db.query(UserSession)
        .filter(
            UserSession.token_hash == token_hash,
            UserSession.revoked.is_(False),
            UserSession.expires_at > datetime.utcnow(),
        )
        .first()
    )
    if session:
        session.last_used_at = datetime.utcnow()
        db.commit()
    return session


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Session"},
    )
    token = _get_cookie_token(request)
    session = _get_valid_session(db, token)
    if not session:
        raise credentials_exception
    user = session.user
    if not user or not user.is_active:
        raise credentials_exception
    request.state.session = session
    return user


def get_current_user_ws(
    websocket, db: Session = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    token = websocket.cookies.get("session")
    session = _get_valid_session(db, token)
    if not session:
        raise credentials_exception
    user = session.user
    if not user or not user.is_active:
        raise credentials_exception
    return user


_WS_SCHEME_MAP = {"ws": "http", "wss": "https"}


def check_origin(request: Request) -> None:
    """Validate Origin/Referer for mutating and WebSocket requests."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        # Same-origin browser requests should always send Origin on POST etc.
        # Allow same-origin GETs without Origin.
        if getattr(request, "method", None) in ("POST", "PUT", "PATCH", "DELETE"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing Origin/Referer header",
            )
        return
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    expected_origin = os.getenv("APP_BASE_URL")
    if expected_origin:
        expected = urlparse(expected_origin)
        expected_scheme = expected.scheme
        expected_netloc = expected.netloc
    else:
        # WebSocket scopes use ws:/wss: schemes while the browser Origin
        # header is http:/https:. Compare the HTTP equivalent.
        expected_scheme = _WS_SCHEME_MAP.get(request.url.scheme, request.url.scheme)
        expected_netloc = request.url.netloc
    if parsed.scheme != expected_scheme or parsed.netloc != expected_netloc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin request rejected",
        )


def logout_session(db: Session, token: Optional[str]) -> None:
    session = _get_valid_session(db, token)
    if session:
        session.revoked = True
        db.commit()


def revoke_all_user_sessions(db: Session, user_id: int) -> None:
    db.query(UserSession).filter(
        UserSession.user_id == user_id, UserSession.revoked.is_(False)
    ).update({"revoked": True}, synchronize_session=False)
    db.commit()


def validate_csrf_and_origin(request: Request, db: Session) -> None:
    """Validate Origin and CSRF token for mutating requests.

    Called by global middleware for all state-changing endpoints.
    Login is exempted from CSRF because the session has not been created yet.
    """
    check_origin(request)
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    if request.url.path == "/api/auth/login":
        return
    token = request.cookies.get("session")
    session = _get_valid_session(db, token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed: no active session",
        )
    submitted = request.headers.get("x-csrf-token")
    if not submitted or not secrets.compare_digest(submitted, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed: invalid token",
        )
