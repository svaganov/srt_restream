"""Import/export configuration tests."""
import json


def _import(client, auth_headers, payload, mode="append"):
    return client.post(
        f"/api/import?mode={mode}",
        files={"file": ("cfg.json", json.dumps(payload), "application/json")},
        **auth_headers,
    )


def test_export_structure_and_no_passphrases(client, auth_headers):
    r = client.get("/api/export", cookies=auth_headers["cookies"])
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    data = json.loads(r.text)
    assert data["version"] == 1
    assert "inputs" in data
    assert "passphrase" not in r.text


def test_import_valid(client, auth_headers):
    payload = {"version": 1, "inputs": [{
        "name": "Imp", "srt_url": "srt://0.0.0.0:5050?mode=listener",
        "outputs": [{"name": "O", "srt_url": "srt://0.0.0.0:5055?mode=listener"}],
    }]}
    r = _import(client, auth_headers, payload)
    assert r.status_code == 200
    assert r.json()["created_inputs"] == 1
    assert r.json()["created_outputs"] == 1


def test_import_listener_port_out_of_range_is_422(client, auth_headers):
    payload = {"version": 1, "inputs": [{
        "name": "B", "srt_url": "srt://0.0.0.0:5051?mode=listener",
        "outputs": [{"name": "O", "srt_url": "srt://0.0.0.0:10101?mode=listener"}],
    }]}
    r = _import(client, auth_headers, payload)
    assert r.status_code == 422
    assert "outside the allowed range" in r.json()["detail"]


def test_import_non_srt_scheme_is_422(client, auth_headers):
    payload = {"version": 1, "inputs": [{"name": "B", "srt_url": "http://x:5000?mode=listener"}]}
    r = _import(client, auth_headers, payload)
    assert r.status_code == 422


def test_import_mode_conflict_is_422(client, auth_headers):
    payload = {"version": 1, "inputs": [{
        "name": "B", "srt_url": "srt://0.0.0.0:5052?mode=listener",
        "outputs": [{"name": "O", "srt_url": "srt://0.0.0.0:5053?mode=listener", "mode": "caller"}],
    }]}
    r = _import(client, auth_headers, payload)
    assert r.status_code == 422
    assert "conflicts with URL mode" in r.json()["detail"]


def test_import_malformed_json_is_400(client, auth_headers):
    r = client.post(
        "/api/import?mode=append",
        files={"file": ("cfg.json", "{broken", "application/json")},
        **auth_headers,
    )
    assert r.status_code == 400


def test_export_import_roundtrip_replace(client, auth_headers):
    r = client.get("/api/export", cookies=auth_headers["cookies"])
    payload = json.loads(r.text)
    r = _import(client, auth_headers, payload, mode="replace")
    assert r.status_code == 200
