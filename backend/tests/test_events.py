"""Events bus and events API tests."""
from events import EventBus, event_bus


def test_emit_and_list_order():
    bus = EventBus(maxlen=10)
    bus.emit("info", "input", "stream_started", "first", stream_id=1)
    bus.emit("warning", "output", "stream_stopped", "second", stream_id=2)
    items = bus.list()
    assert [i["message"] for i in items] == ["first", "second"]
    assert items[0]["id"] < items[1]["id"]
    assert items[0]["category"] == "input"
    assert items[1]["source"] == "system"


def test_ring_buffer_maxlen():
    bus = EventBus(maxlen=5)
    for i in range(10):
        bus.emit("info", "system", "tick", f"m{i}")
    items = bus.list(limit=10)
    assert len(items) == 5
    assert items[0]["message"] == "m5"
    assert items[-1]["message"] == "m9"


def test_subscribe_receives_events():
    bus = EventBus()
    received = []
    bus.subscribe(received.append)
    bus.emit("error", "input", "stream_failed", "boom")
    assert len(received) == 1
    assert received[0]["event"] == "stream_failed"
    bus.unsubscribe(received.append)
    bus.emit("info", "input", "x", "y")
    assert len(received) == 1


def test_events_endpoint_requires_session(client):
    client.cookies.clear()
    r = client.get("/api/events")
    assert r.status_code == 401


def test_events_endpoint_returns_list(client, auth_headers):
    r = client.get("/api/events?limit=50", cookies=auth_headers["cookies"])
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_user_action_emits_event(client, auth_headers):
    # Create an input and start it: both actions must be attributed to the user.
    r = client.post(
        "/api/inputs",
        json={"name": "EvtIn", "srt_url": "srt://0.0.0.0:5007?mode=listener"},
        **auth_headers,
    )
    assert r.status_code == 200
    stream_id = r.json()["id"]

    r = client.post(f"/api/inputs/{stream_id}/start", **auth_headers)
    assert r.status_code == 202

    events = client.get("/api/events?limit=100", cookies=auth_headers["cookies"]).json()
    started = [e for e in events if e["event"] == "stream_started" and e["stream_id"] == stream_id]
    assert started, "stream_started event not found"
    assert started[-1]["source"] == "user:admin"
    assert started[-1]["stream_name"] == "EvtIn"

    r = client.post(f"/api/inputs/{stream_id}/stop", **auth_headers)
    assert r.status_code == 202
    events = client.get("/api/events?limit=100", cookies=auth_headers["cookies"]).json()
    stopped = [e for e in events if e["event"] == "stream_stopped" and e["stream_id"] == stream_id]
    assert stopped and stopped[-1]["source"] == "user:admin"


def test_restart_all_emits_event(client, auth_headers):
    r = client.post("/api/system/restart-streams", **auth_headers)
    assert r.status_code == 202
    events = client.get("/api/events?limit=100", cookies=auth_headers["cookies"]).json()
    restarts = [e for e in events if e["event"] == "restart_all"]
    assert restarts and restarts[-1]["source"] == "user:admin"


def test_mixer_live_lost_event(test_env, tmp_path):
    from stream_manager import StreamManager

    before = len(event_bus.list())
    mgr = StreamManager(data_dir=str(tmp_path / "d"))
    try:
        mgr._desired_inputs[1] = {"srt_url": "srt://0.0.0.0:5000?mode=listener",
                                   "passphrase_encrypted": None, "generation": 1, "name": "In 1"}
        mgr._on_live_lost(1)
        events = event_bus.list()[before:]
        lost = [e for e in events if e["event"] == "live_lost"]
        assert lost
        assert lost[-1]["source"] == "mixer"
        assert lost[-1]["stream_name"] == "In 1"
    finally:
        mgr.shutdown()
