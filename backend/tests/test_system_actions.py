"""System actions: restart all streams / kill orphan processes."""
from stream_manager import StreamManager


def test_restart_all_endpoint(client, auth_headers):
    r = client.post("/api/system/restart-streams", **auth_headers)
    assert r.status_code == 202
    data = r.json()
    assert "stopped_inputs" in data
    assert "stopped_outputs" in data


def test_kill_orphans_endpoint(client, auth_headers):
    r = client.post("/api/system/kill-orphans", **auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "killed" in data
    assert isinstance(data["killed"], list)


def test_restart_all_preserves_desired_state(tmp_path, test_env):
    mgr = StreamManager(data_dir=str(tmp_path / "d"))
    try:
        mgr._desired_inputs[1] = {"srt_url": "srt://0.0.0.0:5000?mode=listener",
                                   "passphrase_encrypted": None, "generation": 1}
        result = mgr.restart_all()
        assert result["stopped_inputs"] == 0  # nothing was running
        # Desired state must survive so the supervisor can respawn.
        assert 1 in mgr._desired_inputs
    finally:
        mgr.shutdown()


def test_signature_matching(test_env):
    mgr = StreamManager(data_dir=str(test_env / "d2"))
    rng = "5000-5008,6000-10100"
    try:
        # Our internal loopback port (test env allocator range is 42000-42100)
        assert mgr._matches_our_signature(
            "ffmpeg -i udp://127.0.0.1:42003?fifo_size=1", rng) is True
        # Our SRT listener ranges (both sub-ranges)
        assert mgr._matches_our_signature(
            "ffmpeg -i srt://0.0.0.0:5000?mode=listener", rng) is True
        assert mgr._matches_our_signature(
            "ffmpeg -i srt://0.0.0.0:6001?mode=listener", rng) is True
        # Our slate path
        assert mgr._matches_our_signature(
            "ffmpeg -i data\\slates\\input_1.jpg", rng) is True
        # Foreign ffmpeg: other ports, other paths — must NOT match
        assert mgr._matches_our_signature(
            "ffmpeg -i udp://127.0.0.1:1234 -f mpegts out.ts", rng) is False
        assert mgr._matches_our_signature(
            "ffmpeg -i srt://example.com:5500?mode=caller", rng) is False
        assert mgr._matches_our_signature(
            "ffmpeg -i srt://example.com:5353?mode=caller", rng) is False
        assert mgr._matches_our_signature(
            "ffmpeg -i movie.mp4 out.mkv", rng) is False
    finally:
        mgr.shutdown()
