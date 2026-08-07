"""Stream process manager using FFmpeg.

Single-flight supervisor model:
- Desired state (in-memory, mirroring DB desired_active) is reconciled by one
  supervisor loop. There are no independent restart callbacks, watchers or
  health loops.
- Every stream has a generation token; pending restarts for an old generation
  are cancelled, so Stop/Delete can never resurrect a process.
- Restart backoff: 1/2/4/8/16/30s with jitter; attempts reset only after
  60 seconds of stable uptime.
- Internal UDP ports come from a centralized allocator and are bound before
  the API reports success. All internal sockets bind to 127.0.0.1 only.
"""
import os
import random
import re
import select
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from encryption import decrypt
from events import event_bus

# Restart backoff schedule (seconds) and the uptime that resets the counter.
BACKOFF_SCHEDULE = [1, 2, 4, 8, 16, 30]
STABLE_UPTIME_SECONDS = 60.0

# Live is considered lost after this much silence; then the mixer switches to slate.
LIVE_TIMEOUT_SECONDS = 2.0


def _inject_passphrase(url: str, passphrase: Optional[str]) -> str:
    """Append passphrase query parameter to an SRT URL."""
    if not passphrase:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}passphrase={passphrase}"


def _redact_url(url: str) -> str:
    """Remove passphrase values from URLs for logging."""
    return re.sub(r"passphrase=[^&]*", "passphrase=<redacted>", url, flags=re.IGNORECASE)


class FFmpegProcess:
    """A single FFmpeg child process running in its own process group."""

    def __init__(self, stream_id: int, cmd: list, is_input: bool = True):
        self.stream_id = stream_id
        self.cmd = cmd
        self.is_input = is_input
        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.status = "disconnected"
        self.status_message = ""
        self.start_time: Optional[datetime] = None
        self.last_activity = time.time()
        self.last_data_time: Optional[float] = None
        self.is_listener = any("mode=listener" in arg.lower() for arg in cmd)
        # Codec profile detected from FFmpeg's input "Stream #0:..." banner.
        # FFmpeg repeats the same "Stream #0:" lines for the output banner, so
        # counting stops once "Stream mapping:" appears.
        self.video_codec: Optional[str] = None
        self.audio_codec: Optional[str] = None
        self.video_stream_count = 0
        self.audio_stream_count = 0
        self._streams_finalized = False
        self.stats = {
            "bitrate": "0 kb/s",
            "speed": "0x",
            "frame": 0,
            "fps": 0.0,
            "drop": 0,
            "dup": 0,
        }
        self._stop_event = threading.Event()
        self._last_stderr = deque(maxlen=30)

    def start(self):
        if self.process and self.process.poll() is None:
            return False

        self._stop_event.clear()
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            self.process = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
                **popen_kwargs,
            )
            self.start_time = datetime.now()
            self.status = "ready"
            if not self.is_input and self.is_listener:
                self.status_message = "Waiting for consumer"
            else:
                self.status_message = "Waiting for data"
            self.thread = threading.Thread(target=self._monitor, daemon=True)
            self.thread.start()
            return True
        except Exception as e:
            self.status = "disconnected"
            self.status_message = str(e)
            return False

    def stop(self):
        self._stop_event.set()
        if self.process:
            try:
                self._terminate_group(timeout=3)
            except Exception:
                try:
                    self._kill_group(timeout=1)
                except Exception:
                    pass
            self.process = None
        self.status = "disconnected"
        self.status_message = "Stopped by user"

    def _terminate_group(self, timeout: float):
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            proc.terminate()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(timeout=1)

    def _kill_group(self, timeout: float):
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                return
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def uptime(self) -> float:
        return (datetime.now() - self.start_time).total_seconds() if self.start_time else 0

    def _monitor(self):
        """Monitor stderr for stats, codec profile and errors."""
        if not self.process or not self.process.stderr:
            return

        for line in self.process.stderr:
            if self._stop_event.is_set():
                break

            self.last_activity = time.time()
            line = line.strip()
            self._last_stderr.append(line)

            # Parse FFmpeg progress stats (real data is flowing)
            if "bitrate=" in line:
                self._parse_stats(line)
                if self.status != "connected":
                    self.status = "connected"
                    self.status_message = "Stream active"

            # Parse stream layout for the slate compatibility check.
            if "Stream mapping:" in line:
                self._streams_finalized = True
            if "Stream #" in line and not self._streams_finalized:
                self._parse_stream_line(line)

            # Detect errors
            if "error" in line.lower():
                if "Connection refused" in line:
                    self.status = "warning"
                    self.status_message = "Connection refused"
                elif "Connection timed out" in line:
                    self.status = "warning"
                    self.status_message = "Connection timeout"
                elif "Broken pipe" in line:
                    self.status = "warning"
                    self.status_message = "Connection broken"

            # Input stream: source actually connected
            if self.is_input and "Input #0" in line:
                self.status = "connected"
                self.status_message = "Stream active"

            # Output stream: FFmpeg initialized its pipeline
            if not self.is_input and ("Output #0" in line or "Stream mapping:" in line):
                if self.is_listener:
                    if self.status != "connected":
                        self.status = "ready"
                        self.status_message = "Waiting for consumer"
                else:
                    self.status = "connected"
                    self.status_message = "Stream active"

        if not self._stop_event.is_set():
            self.status = "disconnected"
            self.status_message = "Process exited unexpectedly"
            print(f"[FFMPEG {self.stream_id}] Unexpected exit. Last stderr lines:")
            for l in self._last_stderr:
                print(f"    {_redact_url(l)}")
            tail = " | ".join(_redact_url(l) for l in list(self._last_stderr)[-5:])
            event_bus.emit(
                "error",
                "input" if self.is_input else "output",
                "stream_failed",
                f"Process exited unexpectedly. {tail}" if tail else "Process exited unexpectedly",
                stream_id=self.stream_id,
                source="system",
            )

        self.process = None

    def _parse_stream_line(self, line: str):
        # Ignore output-banner duplicates: count only the input banner,
        # i.e. lines printed before "Stream mapping:".
        if self._streams_finalized:
            return
        # Example: "Stream #0:0: Video: h264 (High) (...)" / "Stream #0:1(eng): Audio: aac"
        m = re.search(r"Stream #0:\d+.*?(Video|Audio)\s*:\s*([a-zA-Z0-9_]+)", line)
        if not m:
            return
        kind, codec = m.group(1), m.group(2).lower()
        if kind == "Video":
            self.video_stream_count += 1
            if self.video_codec is None:
                self.video_codec = codec
        else:
            self.audio_stream_count += 1
            if self.audio_codec is None:
                self.audio_codec = codec

    def _parse_stats(self, line: str):
        patterns = {
            "bitrate": r"bitrate=\s*([\d\.]+\s*\w+/s)",
            "speed": r"speed=\s*([\d\.]+x)",
            "frame": r"frame=\s*(\d+)",
            "fps": r"fps=\s*([\d\.]+)",
            "drop": r"drop=\s*(\d+)",
            "dup": r"dup=\s*(\d+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, line)
            if match:
                try:
                    if key in ["frame", "drop", "dup"]:
                        self.stats[key] = int(match.group(1))
                    elif key == "fps":
                        self.stats[key] = float(match.group(1))
                    else:
                        self.stats[key] = match.group(1)
                except Exception:
                    pass

        if "bitrate=" in line:
            self.last_data_time = time.time()


class PortAllocator:
    """Centralized allocator for internal (loopback-only) UDP ports."""

    def __init__(self, start: int = 40000, end: int = 49999):
        self._lock = threading.Lock()
        self._free = deque(range(start, end + 1))
        self._used = set()

    def acquire(self) -> int:
        with self._lock:
            if not self._free:
                raise RuntimeError("Internal UDP port range exhausted")
            port = self._free.popleft()
            self._used.add(port)
            return port

    def release(self, port: Optional[int]):
        if port is None:
            return
        with self._lock:
            if port in self._used:
                self._used.discard(port)
                self._free.append(port)


class UDPFeedMixer:
    """Forward either the live or the slate feed to the output relay port.

    Binds its receive sockets in start() so bind failures are reported to the
    caller synchronously (never after a successful API response).
    """

    def __init__(self, stream_id: int, live_port: int, slate_port: int,
                 mixed_port: int, thumb_port: int,
                 on_live_start=None, on_live_lost=None):
        self.stream_id = stream_id
        self.live_port = live_port
        self.slate_port = slate_port
        self.mixed_port = mixed_port
        self.thumb_port = thumb_port
        self.on_live_start = on_live_start
        self.on_live_lost = on_live_lost
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._live_sock: Optional[socket.socket] = None
        self._slate_sock: Optional[socket.socket] = None
        self._out_sock: Optional[socket.socket] = None
        self.live_active = False
        self.last_live_time = 0.0

    @staticmethod
    def _open_rx(port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        sock.setblocking(False)
        # Internal media plane must never be reachable from outside the host.
        sock.bind(("127.0.0.1", port))
        return sock

    def start(self):
        """Bind sockets and start the forward loop. Raises on bind failure."""
        if self._thread and self._thread.is_alive():
            return
        try:
            self._live_sock = self._open_rx(self.live_port)
            self._slate_sock = self._open_rx(self.slate_port)
            self._out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except Exception:
            self._close_socks()
            raise
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self._close_socks()

    def _close_socks(self):
        for sock in (self._live_sock, self._slate_sock, self._out_sock):
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
        self._live_sock = None
        self._slate_sock = None
        self._out_sock = None

    def _run(self):
        live_sock = self._live_sock
        slate_sock = self._slate_sock
        out_sock = self._out_sock
        mixed_addr = ("127.0.0.1", self.mixed_port)
        thumb_addr = ("127.0.0.1", self.thumb_port)

        while not self._stop_event.is_set():
            try:
                readable, _, _ = select.select([live_sock, slate_sock], [], [], 0.01)
            except Exception:
                continue

            now = time.time()
            if self.live_active and now - self.last_live_time > LIVE_TIMEOUT_SECONDS:
                self.live_active = False
                print(f"[MIXER {self.stream_id}] Live lost, switching to slate")
                if self.on_live_lost:
                    try:
                        self.on_live_lost(self.stream_id)
                    except Exception as e:
                        print(f"[MIXER {self.stream_id}] on_live_lost error: {e}")

            for sock in readable:
                try:
                    data = sock.recv(65535)
                except Exception:
                    continue
                if sock is live_sock:
                    self.last_live_time = now
                    if not self.live_active:
                        self.live_active = True
                        print(f"[MIXER {self.stream_id}] Live feed detected")
                        if self.on_live_start:
                            try:
                                self.on_live_start(self.stream_id)
                            except Exception as e:
                                print(f"[MIXER {self.stream_id}] on_live_start error: {e}")
                    for addr in (mixed_addr, thumb_addr):
                        try:
                            out_sock.sendto(data, addr)
                        except Exception:
                            pass
                elif sock is slate_sock:
                    if not self.live_active:
                        # Slate feeds both the outputs and the preview thumbnail,
                        # so the dashboard shows the placeholder when the
                        # source is gone instead of a frozen last frame.
                        for addr in (mixed_addr, thumb_addr):
                            try:
                                out_sock.sendto(data, addr)
                            except Exception:
                                pass


class UDPSplitter:
    """Duplicate the mixed feed to each output's own UDP port."""

    def __init__(self, stream_id: int, mixed_port: int):
        self.stream_id = stream_id
        self.mixed_port = mixed_port
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._in_sock: Optional[socket.socket] = None
        self._out_sock: Optional[socket.socket] = None
        self.outputs: Dict[int, int] = {}  # output_id -> out_port

    def start(self):
        """Bind the input socket and start forwarding. Raises on bind failure."""
        if self._thread and self._thread.is_alive():
            return
        try:
            self._in_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._in_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._in_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            self._in_sock.setblocking(False)
            # Internal media plane must never be reachable from outside the host.
            self._in_sock.bind(("127.0.0.1", self.mixed_port))
            self._out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except Exception:
            self._close_socks()
            raise
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self._close_socks()

    def _close_socks(self):
        for sock in (self._in_sock, self._out_sock):
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
        self._in_sock = None
        self._out_sock = None

    def register(self, output_id: int, out_port: int):
        with self._lock:
            self.outputs[output_id] = out_port

    def unregister(self, output_id: int):
        with self._lock:
            self.outputs.pop(output_id, None)

    def _run(self):
        in_sock = self._in_sock
        out_sock = self._out_sock

        while not self._stop_event.is_set():
            try:
                readable, _, _ = select.select([in_sock], [], [], 0.01)
            except Exception:
                continue

            if not readable:
                continue

            try:
                data = in_sock.recv(65535)
            except Exception:
                continue

            with self._lock:
                ports = list(self.outputs.values())

            for port in ports:
                try:
                    out_sock.sendto(data, ("127.0.0.1", port))
                except Exception:
                    pass


class InputContext:
    """All runtime state for one input stream."""

    def __init__(self, stream_id: int, live_port: int, slate_port: int,
                 mixed_port: int, thumb_port: int):
        self.stream_id = stream_id
        self.live_port = live_port
        self.slate_port = slate_port
        self.mixed_port = mixed_port
        self.thumb_port = thumb_port
        self.mixer: Optional[UDPFeedMixer] = None
        self.splitter: Optional[UDPSplitter] = None
        self.relay: Optional[FFmpegProcess] = None
        self.slate: Optional[FFmpegProcess] = None
        self.thumbnail: Optional[FFmpegProcess] = None
        # Slate is enabled only after a compatible single-track H.264/AAC profile
        # is detected; until proven otherwise we assume compatibility.
        self.slate_available = True

    def ports(self) -> List[int]:
        return [self.live_port, self.slate_port, self.mixed_port, self.thumb_port]


class BackoffState:
    def __init__(self):
        self.attempt = 0
        self.next_at = 0.0
        self.last_spawn_at = 0.0


class StreamManager:
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = os.getenv("DATA_DIR")
        if data_dir is None:
            data_dir = Path(__file__).resolve().parent.parent / "data"
        self.data_dir = str(data_dir)
        self.thumbnails_dir = os.path.join(self.data_dir, "thumbnails")
        self.slates_dir = os.path.join(self.data_dir, "slates")
        os.makedirs(self.thumbnails_dir, exist_ok=True)
        os.makedirs(self.slates_dir, exist_ok=True)

        port_start = int(os.getenv("INTERNAL_PORT_START", "40000"))
        port_end = int(os.getenv("INTERNAL_PORT_END", "49999"))
        self.allocator_start = port_start
        self.allocator_end = port_end
        self.allocator = PortAllocator(port_start, port_end)

        # Desired state: what the operator asked to be running.
        self._desired_inputs: Dict[int, dict] = {}
        self._desired_outputs: Dict[int, dict] = {}

        # Actual runtime state.
        self._input_ctx: Dict[int, InputContext] = {}
        self._output_procs: Dict[int, FFmpegProcess] = {}
        self._output_ports: Dict[int, int] = {}  # output_id -> loopback port

        self._backoff: Dict[tuple, BackoffState] = {}
        self._spawning = set()
        self._generations: Dict[tuple, int] = {}

        # Reentrant: the supervisor loop calls _ensure_slate/_ensure_thumbnail
        # while holding the lock, and those helpers take it again.
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._supervisor_thread = threading.Thread(target=self._supervisor_loop, daemon=True)
        self._supervisor_thread.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self):
        """Stop the supervisor and kill every child process group."""
        self._stop_event.set()
        if self._supervisor_thread:
            self._supervisor_thread.join(timeout=5)
        with self._lock:
            self._desired_inputs.clear()
            self._desired_outputs.clear()
            for sid in list(self._input_ctx.keys()):
                self._teardown_input_locked(sid)
            for oid in list(self._output_procs.keys()):
                self._teardown_output_locked(oid)

    # ------------------------------------------------------------------
    # Operator actions: restart / orphan cleanup
    # ------------------------------------------------------------------

    def restart_all(self) -> dict:
        """Tear down every runtime process; the supervisor respawns desired streams.

        Desired state is preserved; backoff is reset so respawn is immediate.
        """
        with self._lock:
            inputs = len(self._input_ctx)
            outputs = len(self._output_procs)
            for sid in list(self._input_ctx.keys()):
                self._teardown_input_locked(sid)
            for oid in list(self._output_procs.keys()):
                self._teardown_output_locked(oid)
            self._backoff.clear()
            self._spawning.clear()
        print(f"[SYSTEM] Restart all: stopped {inputs} inputs, {outputs} outputs; supervisor respawns desired streams")
        return {"stopped_inputs": inputs, "stopped_outputs": outputs}

    def _owned_pids(self) -> set:
        """PIDs of every FFmpeg process currently owned by the manager."""
        pids = set()
        with self._lock:
            for ctx in self._input_ctx.values():
                for proc in (ctx.relay, ctx.slate, ctx.thumbnail):
                    if proc and proc.process:
                        pids.add(proc.process.pid)
            for proc in self._output_procs.values():
                if proc.process:
                    pids.add(proc.process.pid)
        return pids

    def find_orphan_processes(self) -> list:
        """FFmpeg processes that look like ours but are not owned by the manager.

        A process counts as an orphan when its parent is gone AND its command
        line matches our signatures (internal loopback ports, SRT listener
        range, or our slate/thumbnail paths). Other applications' FFmpeg
        processes are never matched.
        """
        try:
            import psutil
        except ImportError:
            return []

        owned = self._owned_pids()
        listener_range = self._listener_range()
        orphans = []
        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            try:
                name = (proc.info["name"] or "").lower()
                if "ffmpeg" not in name:
                    continue
                pid = proc.info["pid"]
                if pid in owned:
                    continue
                # True orphan: parent process is gone.
                ppid = proc.info["ppid"]
                if ppid and psutil.pid_exists(ppid):
                    continue
                cmdline = " ".join(proc.info["cmdline"] or [])
                if not self._matches_our_signature(cmdline, listener_range):
                    continue
                orphans.append({"pid": pid, "cmdline": cmdline[:200]})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return orphans

    def kill_orphans(self) -> dict:
        """Terminate orphaned FFmpeg processes left by previous app generations."""
        try:
            import psutil
        except ImportError:
            return {"killed": [], "error": "psutil not installed"}

        orphans = self.find_orphan_processes()
        killed = []
        for item in orphans:
            try:
                proc = psutil.Process(item["pid"])
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except psutil.TimeoutExpired:
                    proc.kill()
                killed.append(item["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed:
            print(f"[SYSTEM] Killed {len(killed)} orphan FFmpeg processes: {killed}")
        return {"killed": killed, "found": len(orphans)}

    def _listener_range(self) -> str:
        from srt_url import DEFAULT_LISTENER_PORT_RANGE
        return os.getenv("SRT_LISTENER_PORT_RANGE", DEFAULT_LISTENER_PORT_RANGE)

    def _matches_our_signature(self, cmdline: str, listener_range: str) -> bool:
        """Conservative match: only processes that reference OUR resources."""
        from srt_url import port_in_ranges

        # Internal loopback media-plane ports.
        for m in re.finditer(r"udp://127\.0\.0\.1:(\d+)", cmdline):
            port = int(m.group(1))
            if self.allocator_start <= port <= self.allocator_end:
                return True
        # SRT listener ports from the configured public range(s).
        for m in re.finditer(r"srt://[^\s]*:(\d+)", cmdline):
            port = int(m.group(1))
            if port_in_ranges(port, listener_range):
                return True
        # Our slate/thumbnail paths (input_N.jpg naming is app-specific).
        normalized = cmdline.replace("\\", "/")
        if "slates/input_" in normalized or "thumbnails/input_" in normalized:
            return True
        return False

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _slate_image_path(self, stream_id: int) -> str:
        return os.path.join(self.slates_dir, f"input_{stream_id}.jpg")

    def _thumbnail_path(self, stream_id: int) -> str:
        return os.path.join(self.thumbnails_dir, f"input_{stream_id}.jpg")

    # ------------------------------------------------------------------
    # FFmpeg command builders
    # ------------------------------------------------------------------

    def build_input_cmd(self, stream_id: int, srt_url: str, live_port: int,
                        passphrase: Optional[str] = None) -> list:
        """Relay the whole input (every track) to the internal live port."""
        input_url = _inject_passphrase(srt_url, passphrase)
        return [
            "ffmpeg", "-y", "-fflags", "nobuffer", "-flags", "low_delay",
            "-i", input_url,
            "-map", "0", "-c", "copy",
            "-f", "mpegts", f"udp://127.0.0.1:{live_port}?pkt_size=1316",
        ]

    def build_thumbnail_cmd(self, stream_id: int, thumb_port: int) -> list:
        """Best-effort keyframe capture from the live feed.

        Runs as an independent process so audio-only inputs or JPEG errors
        never stop the relay.
        """
        thumbnail_path = self._thumbnail_path(stream_id)
        return [
            "ffmpeg", "-y", "-fflags", "nobuffer", "-flags", "low_delay",
            "-i", f"udp://127.0.0.1:{thumb_port}?fifo_size=1000000&overrun_nonfatal=1",
            "-vf", "fps=1/3,scale=320:-1,format=yuvj420p",
            "-update", "1", "-q:v", "2", thumbnail_path,
        ]

    def _default_slate_image_path(self) -> str:
        """App-wide default slate image, shipped with the frontend assets."""
        return os.getenv(
            "DEFAULT_SLATE_IMAGE",
            str(Path(__file__).resolve().parent.parent
                / "frontend" / "static" / "img" / "placeholder.jpg"),
        )

    def build_slate_cmd(self, stream_id: int, slate_port: int) -> list:
        """Generate a placeholder feed with real-time pacing.

        Image priority: per-input uploaded slate, then the app-wide default
        placeholder, then a plain black frame.
        """
        slate_image = self._slate_image_path(stream_id)
        if not os.path.exists(slate_image):
            default_image = self._default_slate_image_path()
            slate_image = default_image if os.path.exists(default_image) else None

        if slate_image:
            video_input = ["-re", "-f", "image2", "-loop", "1", "-framerate", "30", "-i", slate_image]
            video_filter = "fps=30,format=yuv420p,scale=1280:720:flags=lanczos"
        else:
            video_input = ["-re", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30"]
            video_filter = "fps=30,format=yuv420p"

        return [
            "ffmpeg", "-y",
            *video_input,
            "-re", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", video_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-r", "30", "-g", "25", "-keyint_min", "25",
            "-b:v", "2000k",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "mpegts", f"udp://127.0.0.1:{slate_port}?pkt_size=1316",
        ]

    def build_output_cmd(self, stream_id: int, output_id: int, out_port: int,
                         srt_url: str, passphrase: Optional[str] = None) -> list:
        """Relay the mixed feed to the consumer SRT endpoint."""
        output_url = _inject_passphrase(srt_url, passphrase)
        return [
            "ffmpeg", "-y", "-fflags", "nobuffer", "-flags", "low_delay",
            "-thread_queue_size", "512",
            "-i", f"udp://127.0.0.1:{out_port}?fifo_size=1000000&overrun_nonfatal=1",
            "-map", "0", "-c", "copy",
            "-f", "mpegts", output_url,
        ]

    # ------------------------------------------------------------------
    # Slate compatibility
    # ------------------------------------------------------------------

    @staticmethod
    def _is_slate_compatible(proc: FFmpegProcess) -> bool:
        """Slate seamlessness is only promised for single-track H.264/AAC inputs."""
        if proc.video_stream_count == 0:
            # No streams parsed yet — keep assuming compatibility.
            return True
        if proc.video_stream_count != 1:
            return False
        if proc.video_codec not in ("h264",):
            return False
        if proc.audio_stream_count > 1:
            return False
        if proc.audio_stream_count == 1 and proc.audio_codec not in ("aac",):
            return False
        return True

    # ------------------------------------------------------------------
    # Public API: inputs
    # ------------------------------------------------------------------

    def start_input(self, stream_id: int, srt_url: str,
                    passphrase_encrypted: Optional[str] = None,
                    name: Optional[str] = None) -> bool:
        passphrase = decrypt(passphrase_encrypted) if passphrase_encrypted else None

        with self._lock:
            if stream_id in self._desired_inputs:
                self._desired_inputs[stream_id]["generation"] = \
                    self._bump_generation_locked(("input", stream_id))
                if name:
                    self._desired_inputs[stream_id]["name"] = name
                return True

            generation = self._bump_generation_locked(("input", stream_id))
            self._desired_inputs[stream_id] = {
                "srt_url": srt_url,
                "passphrase_encrypted": passphrase_encrypted,
                "generation": generation,
                "name": name,
            }

            # Allocate ports and bind mixer/splitter synchronously so bind
            # failures fail the API call instead of the background loop.
            try:
                ctx = self._create_input_ctx_locked(stream_id)
            except Exception as e:
                self._desired_inputs.pop(stream_id, None)
                print(f"[INPUT {stream_id}] Failed to bind internal sockets: {e}")
                return False

        ok = self._spawn_input_relay(stream_id, srt_url, passphrase)
        if not ok:
            with self._lock:
                self._desired_inputs.pop(stream_id, None)
                self._teardown_input_locked(stream_id)
        return ok

    def stop_input(self, stream_id: int):
        with self._lock:
            self._bump_generation_locked(("input", stream_id))
            self._desired_inputs.pop(stream_id, None)
            self._teardown_input_locked(stream_id)
            # Stop every desired output of this input as well.
            for oid, desired in list(self._desired_outputs.items()):
                if desired["stream_id"] == stream_id:
                    self._bump_generation_locked(("output", oid))
                    self._desired_outputs.pop(oid, None)
                    self._teardown_output_locked(oid)

    def get_input_status(self, stream_id: int) -> dict:
        with self._lock:
            ctx = self._input_ctx.get(stream_id)
            desired = stream_id in self._desired_inputs

            if ctx and ctx.relay and ctx.relay.is_alive():
                relay = ctx.relay
                slate_alive = ctx.slate and ctx.slate.is_alive()
                if relay.status != "connected" and slate_alive:
                    return {
                        "status": "slate" if ctx.slate_available else "disconnected",
                        "message": "No signal - slate active" if ctx.slate_available else "No signal - slate unavailable",
                        "stats": ctx.slate.stats,
                        "uptime": ctx.slate.uptime(),
                        "slate_available": ctx.slate_available,
                        "desired_state": "active" if desired else "stopped",
                    }
                return {
                    "status": relay.status,
                    "message": relay.status_message,
                    "stats": relay.stats,
                    "uptime": relay.uptime(),
                    "slate_available": ctx.slate_available,
                    "desired_state": "active" if desired else "stopped",
                }

            if ctx and ctx.slate and ctx.slate.is_alive():
                return {
                    "status": "slate",
                    "message": "No signal - slate active",
                    "stats": ctx.slate.stats,
                    "uptime": ctx.slate.uptime(),
                    "slate_available": ctx.slate_available,
                    "desired_state": "active" if desired else "stopped",
                }

            return {
                "status": "disconnected",
                "message": "Not running",
                "stats": {},
                "slate_available": ctx.slate_available if ctx else False,
                "desired_state": "active" if desired else "stopped",
            }

    def get_input_srt_stats(self, stream_id: int) -> dict:
        # srt-live-transmit is intentionally removed from the critical path.
        return {
            "available": False,
            "message": "SRT statistics are not available in this build",
        }

    # ------------------------------------------------------------------
    # Public API: outputs
    # ------------------------------------------------------------------

    def start_output(self, stream_id: int, output_id: int, srt_url: str,
                     passphrase_encrypted: Optional[str] = None,
                     name: Optional[str] = None) -> bool:
        with self._lock:
            if stream_id not in self._input_ctx:
                return False
            generation = self._bump_generation_locked(("output", output_id))
            self._desired_outputs[output_id] = {
                "stream_id": stream_id,
                "srt_url": srt_url,
                "passphrase_encrypted": passphrase_encrypted,
                "generation": generation,
                "name": name,
            }

        ok = self._spawn_output(output_id, stream_id, srt_url, passphrase_encrypted)
        if not ok:
            with self._lock:
                self._desired_outputs.pop(output_id, None)
                self._teardown_output_locked(output_id)
        return ok

    def stop_output(self, output_id: int):
        with self._lock:
            self._bump_generation_locked(("output", output_id))
            self._desired_outputs.pop(output_id, None)
            self._teardown_output_locked(output_id)

    def get_output_status(self, output_id: int) -> dict:
        with self._lock:
            desired = output_id in self._desired_outputs
            proc = self._output_procs.get(output_id)
            if not proc or not proc.is_alive():
                return {
                    "status": "disconnected",
                    "message": "Not running",
                    "stats": {},
                    "desired_state": "active" if desired else "stopped",
                }
            return {
                "status": proc.status,
                "message": proc.status_message,
                "stats": proc.stats,
                "uptime": proc.uptime(),
                "desired_state": "active" if desired else "stopped",
            }

    # ------------------------------------------------------------------
    # Internal: generations
    # ------------------------------------------------------------------

    def _bump_generation_locked(self, key: tuple) -> int:
        gen = self._generations.get(key, 0) + 1
        self._generations[key] = gen
        return gen

    def _generation_locked(self, key: tuple) -> int:
        return self._generations.get(key, 0)

    # ------------------------------------------------------------------
    # Internal: input lifecycle
    # ------------------------------------------------------------------

    def _create_input_ctx_locked(self, stream_id: int) -> InputContext:
        """Allocate ports, bind mixer/splitter, start slate. Raises on failure."""
        if stream_id in self._input_ctx:
            return self._input_ctx[stream_id]

        live_port = self.allocator.acquire()
        slate_port = self.allocator.acquire()
        mixed_port = self.allocator.acquire()
        thumb_port = self.allocator.acquire()
        ctx = InputContext(stream_id, live_port, slate_port, mixed_port, thumb_port)
        try:
            ctx.mixer = UDPFeedMixer(
                stream_id,
                live_port,
                slate_port,
                mixed_port,
                thumb_port,
                on_live_start=self._on_live_start,
                on_live_lost=self._on_live_lost,
            )
            ctx.mixer.start()
            ctx.splitter = UDPSplitter(stream_id, mixed_port)
            ctx.splitter.start()
        except Exception:
            if ctx.mixer:
                ctx.mixer.stop()
            if ctx.splitter:
                ctx.splitter.stop()
            for port in ctx.ports():
                self.allocator.release(port)
            raise

        self._input_ctx[stream_id] = ctx
        return ctx

    def _spawn_input_relay(self, stream_id: int, srt_url: str,
                           passphrase: Optional[str]) -> bool:
        with self._lock:
            ctx = self._input_ctx.get(stream_id)
            if not ctx:
                return False
            if ctx.relay and ctx.relay.is_alive():
                return True
            cmd = self.build_input_cmd(stream_id, srt_url, ctx.live_port, passphrase)
            relay = FFmpegProcess(stream_id, cmd, is_input=True)
            if not relay.start():
                return False
            ctx.relay = relay

        # Start the slate generator so outputs always have a fallback feed.
        self._ensure_slate(stream_id)
        # Start the best-effort thumbnail capture.
        self._ensure_thumbnail(stream_id)
        return True

    def _ensure_slate(self, stream_id: int) -> bool:
        with self._lock:
            ctx = self._input_ctx.get(stream_id)
            if not ctx or not ctx.slate_available:
                return False
            if ctx.slate and ctx.slate.is_alive():
                return True
            cmd = self.build_slate_cmd(stream_id, ctx.slate_port)
            proc = FFmpegProcess(stream_id, cmd, is_input=False)
            if proc.start():
                ctx.slate = proc
                return True
            print(f"[SLATE] Failed to start slate for input {stream_id}: {proc.status_message}")
            return False

    def _stop_slate(self, stream_id: int):
        with self._lock:
            ctx = self._input_ctx.get(stream_id)
            if ctx and ctx.slate:
                ctx.slate.stop()
                ctx.slate = None

    def _ensure_thumbnail(self, stream_id: int) -> bool:
        with self._lock:
            ctx = self._input_ctx.get(stream_id)
            if not ctx:
                return False
            if ctx.thumbnail and ctx.thumbnail.is_alive():
                return True
            cmd = self.build_thumbnail_cmd(stream_id, ctx.thumb_port)
            proc = FFmpegProcess(stream_id, cmd, is_input=False)
            if proc.start():
                ctx.thumbnail = proc
                return True
            return False

    def _teardown_input_locked(self, stream_id: int):
        ctx = self._input_ctx.pop(stream_id, None)
        if not ctx:
            return
        if ctx.relay:
            ctx.relay.stop()
        if ctx.slate:
            ctx.slate.stop()
        if ctx.thumbnail:
            ctx.thumbnail.stop()
        if ctx.mixer:
            ctx.mixer.stop()
        if ctx.splitter:
            ctx.splitter.stop()
        for port in ctx.ports():
            self.allocator.release(port)
        self._backoff.pop(("input", stream_id), None)

    # ------------------------------------------------------------------
    # Internal: output lifecycle
    # ------------------------------------------------------------------

    def _spawn_output(self, output_id: int, stream_id: int, srt_url: str,
                      passphrase_encrypted: Optional[str]) -> bool:
        with self._lock:
            ctx = self._input_ctx.get(stream_id)
            if not ctx:
                return False
            if output_id in self._output_procs and self._output_procs[output_id].is_alive():
                return True

            out_port = self.allocator.acquire()
            ctx.splitter.register(output_id, out_port)
            passphrase = decrypt(passphrase_encrypted) if passphrase_encrypted else None
            cmd = self.build_output_cmd(stream_id, output_id, out_port, srt_url, passphrase)
            proc = FFmpegProcess(output_id, cmd, is_input=False)
            if not proc.start():
                ctx.splitter.unregister(output_id)
                self.allocator.release(out_port)
                return False
            self._output_procs[output_id] = proc
            self._output_ports[output_id] = out_port
            return True

    def _teardown_output_locked(self, output_id: int):
        proc = self._output_procs.pop(output_id, None)
        if proc:
            proc.stop()
        port = self._output_ports.pop(output_id, None)
        for ctx in self._input_ctx.values():
            if ctx.splitter and output_id in ctx.splitter.outputs:
                ctx.splitter.unregister(output_id)
                break
        self.allocator.release(port)
        self._backoff.pop(("output", output_id), None)

    # ------------------------------------------------------------------
    # Mixer callbacks (routed through the supervisor's desired state)
    # ------------------------------------------------------------------

    def _input_name(self, stream_id: int) -> Optional[str]:
        with self._lock:
            desired = self._desired_inputs.get(stream_id, {})
            return desired.get("name")

    def _output_name(self, output_id: int) -> Optional[str]:
        with self._lock:
            desired = self._desired_outputs.get(output_id, {})
            return desired.get("name")

    def _on_live_start(self, stream_id: int):
        """Live source connected: refresh outputs and re-check slate compatibility."""
        with self._lock:
            ctx = self._input_ctx.get(stream_id)
            if ctx and ctx.relay and not self._is_slate_compatible(ctx.relay):
                if ctx.slate_available:
                    ctx.slate_available = False
                    print(f"[SLATE] Input {stream_id} is not slate-compatible, disabling slate")
        event_bus.emit(
            "info", "input", "live_detected",
            "Live source connected",
            stream_id=stream_id, stream_name=self._input_name(stream_id), source="mixer",
        )
        self._stop_slate(stream_id)
        self._request_output_refresh(stream_id)

    def _on_live_lost(self, stream_id: int):
        """Live source lost: restart outputs on the slate feed and re-open the input listener."""
        print(f"[SLATE] Live lost for input {stream_id}, refreshing outputs and input listener")
        event_bus.emit(
            "warning", "input", "live_lost",
            "Live source lost, switching to slate",
            stream_id=stream_id, stream_name=self._input_name(stream_id), source="mixer",
        )
        self._ensure_slate(stream_id)
        self._request_output_refresh(stream_id)
        with self._lock:
            ctx = self._input_ctx.get(stream_id)
            if ctx and ctx.relay and ctx.relay.is_alive():
                # The FFmpeg SRT listener accepts only one caller; recycle it so a
                # reconnecting source can attach.
                ctx.relay.stop()

    def _request_output_refresh(self, stream_id: int):
        """Ask the supervisor to respawn every desired output of this input."""
        with self._lock:
            for oid, desired in list(self._desired_outputs.items()):
                if desired["stream_id"] != stream_id:
                    continue
                # Generation stays the same — the stream is still desired — but
                # the running process is torn down so the supervisor respawns it
                # immediately on the fresh feed.
                self._teardown_output_locked(oid)
                backoff = self._backoff.setdefault(("output", oid), BackoffState())
                backoff.attempt = 0
                backoff.next_at = 0.0

    # ------------------------------------------------------------------
    # Supervisor loop
    # ------------------------------------------------------------------

    def _next_backoff_delay(self, attempt: int) -> float:
        base = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
        return base * random.uniform(0.8, 1.2)

    def _supervisor_loop(self):
        while not self._stop_event.is_set():
            time.sleep(0.5)
            try:
                self._supervisor_tick()
            except Exception:
                import traceback
                print("[SUPERVISOR] Unhandled error (loop continues):")
                traceback.print_exc()

    def _supervisor_tick(self):
            now = time.time()
            to_spawn = []
            with self._lock:
                # Inputs
                for sid, desired in list(self._desired_inputs.items()):
                    ctx = self._input_ctx.get(sid)
                    relay_alive = ctx and ctx.relay and ctx.relay.is_alive()
                    if relay_alive:
                        # Reset the backoff counter only after stable uptime.
                        if ctx.relay.uptime() >= STABLE_UPTIME_SECONDS:
                            bs = self._backoff.get(("input", sid))
                            if bs:
                                bs.attempt = 0
                        # Keep the slate/thumbnail companions alive.
                        if not ctx.slate or not ctx.slate.is_alive():
                            self._ensure_slate(sid)
                        if not ctx.thumbnail or not ctx.thumbnail.is_alive():
                            # Best-effort: only retry occasionally.
                            thumb_bs = self._backoff.setdefault(("thumb", sid), BackoffState())
                            if now >= thumb_bs.next_at:
                                thumb_bs.attempt += 1
                                thumb_bs.next_at = now + self._next_backoff_delay(thumb_bs.attempt)
                                self._ensure_thumbnail(sid)
                    else:
                        bs = self._backoff.setdefault(("input", sid), BackoffState())
                        if now >= bs.next_at:
                            to_spawn.append(("input", sid, desired["generation"], now))

                # Outputs
                for oid, desired in list(self._desired_outputs.items()):
                    proc = self._output_procs.get(oid)
                    if proc and proc.is_alive():
                        if proc.uptime() >= STABLE_UPTIME_SECONDS:
                            bs = self._backoff.get(("output", oid))
                            if bs:
                                bs.attempt = 0
                        continue
                    # Idle listener restart: consumer disconnected, the FFmpeg SRT
                    # listener stopped accepting callers while the process stayed up.
                    if proc and proc.last_data_time is not None and (now - proc.last_data_time > 6.0):
                        self._teardown_output_locked(oid)
                    bs = self._backoff.setdefault(("output", oid), BackoffState())
                    if now >= bs.next_at:
                        to_spawn.append(("output", oid, desired["generation"], now))

            # Spawn outside the lock; single-flight per stream.
            for kind, sid, generation, now in to_spawn:
                key = (kind, sid)
                if key in self._spawning:
                    continue
                bs = self._backoff.setdefault(key, BackoffState())
                bs.attempt += 1
                bs.next_at = now + self._next_backoff_delay(bs.attempt)
                self._spawning.add(key)
                if kind == "input":
                    threading.Thread(
                        target=self._respawn_input,
                        args=(sid, generation),
                        daemon=True,
                    ).start()
                else:
                    threading.Thread(
                        target=self._respawn_output,
                        args=(sid, generation),
                        daemon=True,
                    ).start()

    def _respawn_input(self, stream_id: int, generation: int):
        try:
            with self._lock:
                desired = self._desired_inputs.get(stream_id)
                if not desired or desired["generation"] != generation:
                    return
                # Recreate the context (ports may have been released by teardown).
                ctx = self._input_ctx.get(stream_id)
                if not ctx:
                    try:
                        ctx = self._create_input_ctx_locked(stream_id)
                    except Exception as e:
                        print(f"[SUPERVISOR] Failed to rebind input {stream_id}: {e}")
                        return
                srt_url = desired["srt_url"]
                passphrase_encrypted = desired.get("passphrase_encrypted")
                attempt = self._backoff.get(("input", stream_id), BackoffState()).attempt
            passphrase = decrypt(passphrase_encrypted) if passphrase_encrypted else None
            ok = self._spawn_input_relay(stream_id, srt_url, passphrase)
            event_bus.emit(
                "info" if ok else "warning", "input", "stream_restarted",
                f"Supervisor respawned input (attempt {attempt})" + ("" if ok else " — failed"),
                stream_id=stream_id, stream_name=self._input_name(stream_id), source="supervisor",
            )
        finally:
            self._spawning.discard(("input", stream_id))

    def _respawn_output(self, output_id: int, generation: int):
        try:
            with self._lock:
                desired = self._desired_outputs.get(output_id)
                if not desired or desired["generation"] != generation:
                    return
                stream_id = desired["stream_id"]
                srt_url = desired["srt_url"]
                passphrase_encrypted = desired.get("passphrase_encrypted")
                attempt = self._backoff.get(("output", output_id), BackoffState()).attempt
            ok = self._spawn_output(output_id, stream_id, srt_url, passphrase_encrypted)
            event_bus.emit(
                "info" if ok else "warning", "output", "stream_restarted",
                f"Supervisor respawned output (attempt {attempt})" + ("" if ok else " — failed"),
                stream_id=output_id, stream_name=self._output_name(output_id), source="supervisor",
            )
        finally:
            self._spawning.discard(("output", output_id))


# ---------------------------------------------------------------------------
# Lazy singleton: created by the FastAPI lifespan, never at import time.
# ---------------------------------------------------------------------------

_manager: Optional[StreamManager] = None


def init_stream_manager(data_dir: str = None) -> StreamManager:
    global _manager
    if _manager is None:
        _manager = StreamManager(data_dir)
    return _manager


def shutdown_stream_manager():
    global _manager
    if _manager is not None:
        _manager.shutdown()
        _manager = None


class _LazyStreamManager:
    """Proxy so modules can import `stream_manager` before the lifespan runs."""

    def __getattr__(self, name):
        if _manager is None:
            raise RuntimeError("StreamManager is not initialized; FastAPI lifespan has not run")
        return getattr(_manager, name)


stream_manager = _LazyStreamManager()
