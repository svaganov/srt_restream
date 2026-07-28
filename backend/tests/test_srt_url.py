"""SRT URL validation tests (P0: no arbitrary protocols, strict shape)."""
import pytest

from srt_url import SrtUrl


def test_rejects_non_srt_scheme():
    with pytest.raises(ValueError):
        SrtUrl.parse("http://example.com:5000?mode=listener")


def test_rejects_file_scheme():
    with pytest.raises(ValueError):
        SrtUrl.parse("file:///etc/passwd")


def test_rejects_missing_mode():
    with pytest.raises(ValueError):
        SrtUrl.parse("srt://0.0.0.0:5000")


def test_rejects_invalid_mode():
    with pytest.raises(ValueError):
        SrtUrl.parse("srt://0.0.0.0:5000?mode=whatever")


def test_rejects_userinfo():
    with pytest.raises(ValueError):
        SrtUrl.parse("srt://user:pass@0.0.0.0:5000?mode=listener")


def test_rejects_path():
    with pytest.raises(ValueError):
        SrtUrl.parse("srt://0.0.0.0:5000/some/path?mode=listener")


def test_rejects_passphrase_in_url():
    with pytest.raises(ValueError):
        SrtUrl.parse("srt://0.0.0.0:5000?mode=listener&passphrase=secret")


def test_listener_out_of_range_rejected():
    with pytest.raises(ValueError):
        SrtUrl.parse("srt://0.0.0.0:7000?mode=listener")


def test_caller_loopback_rejected():
    with pytest.raises(ValueError):
        SrtUrl.parse("srt://127.0.0.1:6000?mode=caller")


def test_valid_listener():
    url = SrtUrl.parse("srt://0.0.0.0:5000?mode=listener&latency=200")
    assert url.mode == "listener"
    assert url.port == 5000


def test_valid_caller():
    url = SrtUrl.parse("srt://remote.host:6001?mode=caller")
    assert url.mode == "caller"


def test_api_rejects_non_srt(client, auth_headers):
    r = client.post(
        "/api/inputs",
        json={"name": "X", "srt_url": "http://example.com:5000?mode=listener"},
        **auth_headers,
    )
    assert r.status_code == 422


def test_api_mode_conflict(client, auth_headers):
    r = client.post(
        "/api/outputs",
        json={
            "input_stream_id": 1,
            "name": "X",
            "srt_url": "srt://remote.host:6000?mode=caller",
            "mode": "listener",
        },
        **auth_headers,
    )
    assert r.status_code == 422
