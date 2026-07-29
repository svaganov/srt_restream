"""Supervisor building-block tests (no FFmpeg required)."""
import time

import pytest

from stream_manager import PortAllocator, FFmpegProcess


def test_supervisor_no_deadlock_with_alive_relay(tmp_path, test_env):
    """Regression: the supervisor loop called _ensure_slate/_ensure_thumbnail
    while holding the manager lock, and those helpers took the same
    non-reentrant lock — deadlocking the whole app (server hang on :8080).
    """
    from stream_manager import StreamManager, InputContext

    mgr = StreamManager(data_dir=str(tmp_path / "d"))
    try:
        ctx = InputContext(1, 46101, 46102, 46103, 46104)
        fake = FFmpegProcess(1, ["ffmpeg"], is_input=True)
        fake.is_alive = lambda: True
        fake.uptime = lambda: 0.0
        ctx.relay = fake
        ctx.slate = None
        mgr._input_ctx[1] = ctx
        mgr._desired_inputs[1] = {
            "srt_url": "srt://0.0.0.0:5000?mode=listener",
            "passphrase_encrypted": None,
            "generation": 1,
        }

        time.sleep(1.5)  # let the supervisor tick a few times
        acquired = mgr._lock.acquire(timeout=5)
        assert acquired, "supervisor deadlocked the manager lock"
        mgr._lock.release()
    finally:
        mgr.shutdown()


def test_port_allocator_reuse():
    alloc = PortAllocator(45000, 45002)
    ports = [alloc.acquire() for _ in range(3)]
    assert sorted(ports) == [45000, 45001, 45002]
    with pytest.raises(RuntimeError):
        alloc.acquire()
    alloc.release(ports[1])
    assert alloc.acquire() == 45001


def _feed_banner(proc, video_lines, audio_lines):
    """Simulate FFmpeg stderr: input banner, then mapping, then output banner."""
    for kind, codec in video_lines:
        proc._parse_stream_line(f"    Stream #0:0: Video: {codec} (High), yuv420p")
    for kind, codec in audio_lines:
        proc._parse_stream_line(f"    Stream #0:1: Audio: {codec} (LC), 48000 Hz")
    proc._streams_finalized = True
    # Output banner repeats the same Stream #0 lines — must be ignored.
    for kind, codec in video_lines + audio_lines:
        proc._parse_stream_line(f"    Stream #0:0: Video: h264 (High)")


def test_slate_compatibility_single_track():
    """Single-track H.264/AAC input must stay slate-compatible even though
    FFmpeg prints the Stream #0 lines twice (input + output banners)."""
    from stream_manager import FFmpegProcess, StreamManager

    proc = FFmpegProcess(1, ["ffmpeg"], is_input=True)
    _feed_banner(proc, [("Video", "h264")], [("Audio", "aac")])
    assert proc.video_stream_count == 1
    assert proc.audio_stream_count == 1
    assert StreamManager._is_slate_compatible(proc) is True


def test_slate_compatibility_multi_video_rejected():
    from stream_manager import FFmpegProcess, StreamManager

    proc = FFmpegProcess(1, ["ffmpeg"], is_input=True)
    _feed_banner(proc, [("Video", "h264"), ("Video", "h264")], [("Audio", "aac")])
    assert proc.video_stream_count == 2
    assert StreamManager._is_slate_compatible(proc) is False


def test_ffmpeg_cmd_uses_map_zero_and_loopback(tmp_path, test_env):
    from stream_manager import StreamManager

    mgr = StreamManager(data_dir=str(tmp_path / "data"))
    try:
        cmd = mgr.build_input_cmd(1, "srt://0.0.0.0:5000?mode=listener", 45000)
        assert "-map" in cmd
        assert cmd[cmd.index("-map") + 1] == "0"
        assert "-c" in cmd
        assert cmd[cmd.index("-c") + 1] == "copy"
        assert "udp://127.0.0.1:45000" in " ".join(cmd)
        # No inline thumbnail mapping anymore — thumbnails are separate.
        assert "0:v" not in cmd

        out = mgr.build_output_cmd(1, 1, 45001, "srt://remote.host:6000?mode=caller")
        assert "-map" in out and out[out.index("-map") + 1] == "0"

        slate = mgr.build_slate_cmd(1, 45002)
        # Real-time pacing is mandatory to avoid 18x bursts.
        assert "-re" in slate
    finally:
        mgr.shutdown()


def test_srt_stats_unavailable(test_env):
    from stream_manager import StreamManager

    mgr = StreamManager(data_dir=str(test_env / "data2"))
    try:
        stats = mgr.get_input_srt_stats(1)
        assert stats["available"] is False
    finally:
        mgr.shutdown()
