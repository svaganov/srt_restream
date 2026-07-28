"""Supervisor building-block tests (no FFmpeg required)."""
import pytest

from stream_manager import PortAllocator, FFmpegProcess


def test_port_allocator_reuse():
    alloc = PortAllocator(45000, 45002)
    ports = [alloc.acquire() for _ in range(3)]
    assert sorted(ports) == [45000, 45001, 45002]
    with pytest.raises(RuntimeError):
        alloc.acquire()
    alloc.release(ports[1])
    assert alloc.acquire() == 45001


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
