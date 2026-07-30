"""Authentication and CSRF tests."""


def test_no_origin_login_rejected(client):
    r = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "wrong"},
    )
    assert r.status_code == 403


def test_login_sets_session_and_csrf(client, admin_credentials):
    assert admin_credentials["cookies"]["session"]
    assert admin_credentials["csrf"]


def test_auth_me_requires_session(client):
    client.cookies.clear()
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_auth_me_with_session(client, admin_credentials):
    r = client.get("/api/auth/me", cookies=admin_credentials["cookies"])
    assert r.status_code == 200
    assert r.json()["username"] == "admin"


def test_mutating_without_csrf_rejected(client, admin_credentials):
    r = client.post(
        "/api/inputs",
        json={"name": "X", "srt_url": "srt://0.0.0.0:5000?mode=listener"},
        cookies=admin_credentials["cookies"],
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 403


def test_mutating_with_csrf_ok(client, auth_headers):
    r = client.post(
        "/api/inputs",
        json={"name": "CSRF-OK", "srt_url": "srt://0.0.0.0:5005?mode=listener"},
        **auth_headers,
    )
    assert r.status_code == 200


def test_old_default_credentials_do_not_work(client):
    # admin/admin must never authenticate.
    r = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401


def test_logout_invalidates_session(client, auth_headers):
    # Log out with CSRF, then the session cookie must be rejected.
    r = client.post("/api/auth/logout", **auth_headers)
    assert r.status_code == 200
    r = client.get("/api/auth/me", cookies=auth_headers["cookies"])
    assert r.status_code == 401
