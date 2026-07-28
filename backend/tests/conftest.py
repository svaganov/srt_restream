"""Shared pytest fixtures: isolated SQLite DB + TestClient with lifespan."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(scope="session")
def test_env(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    os.environ["DATABASE_URL"] = f"sqlite:///{data_dir}/test.db"
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["SECRET_KEY"] = "test-secret-key-for-tests-only"
    from cryptography.fernet import Fernet
    os.environ["SECRETS_KEY"] = Fernet.generate_key().decode()
    os.environ["SESSION_COOKIE_SECURE"] = "false"
    os.environ["INTERNAL_PORT_START"] = "42000"
    os.environ["INTERNAL_PORT_END"] = "42100"

    # The lifespan requires an admin user to exist; create it before startup.
    from models import init_db, get_db, User
    from auth import hash_password

    init_db()
    db = next(get_db())
    try:
        if not db.query(User).first():
            db.add(User(username="admin", hashed_password=hash_password("AdminPass123!")))
            db.commit()
    finally:
        db.close()
    return data_dir


@pytest.fixture(scope="session")
def client(test_env):
    from main import app
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session")
def admin_credentials(client, test_env):
    """Log in once per session and expose session + CSRF artifacts."""
    r = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "AdminPass123!"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    return {
        "cookies": {"session": client.cookies.get("session")},
        "csrf": r.json()["csrf_token"],
    }


@pytest.fixture()
def auth_headers(client):
    """Fresh login per test so revoked sessions never leak between tests."""
    import auth
    auth._login_attempts.clear()
    r = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "AdminPass123!"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    return {
        "headers": {
            "Origin": "http://testserver",
            "X-CSRF-Token": r.json()["csrf_token"],
        },
        "cookies": {"session": client.cookies.get("session")},
    }
