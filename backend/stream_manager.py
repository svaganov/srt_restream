"""Stream process manager using FFmpeg"""
import subprocess
import threading
import time
import os
import signal
import re
import socket
import select
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional


class FFmpegProcess:
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
        self.stats = {
            "bitrate": "0 kb/s",
            "speed": "0x",
            "frame": 0,
            "fps": 0.0,
            "drop": 0,
            "dup": 0
        }
        self._stop_event = threading.Event()

    def start(self):
        if self.process and self.process.poll() is None:
            return False

        self._stop_event.clear()
        try:
            self.process = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
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
                self.process.terminate()
                self.process.wait(timeout=3)
            except:
                try:
                    self.process.kill()
                    self.process.wait(timeout=1)
                except:
                    pass
            self.process = None
        self.status = "disconnected"
        self.status_message = "Stopped by user"

    def _monitor(self):
        """Monitor stderr for stats and errors"""
        if not self.process or not self.process.stderr:
            return

        for line in self.process.stderr:
            if self._stop_event.is_set():
                break

            self.last_activity = time.time()
            line = line.strip()

            # Parse FFmpeg progress stats (real data is flowing)
            if "bitrate=" in line:
                self._parse_stats(line)
                if self.status != "connected":
                    self.status = "connected"
                    self.status_message = "Stream active"

            # Detect errors
            if "Error" in line or "error" in line.lower():
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
                    # Listener is up and waiting for a consumer
                    if self.status != "connected":
                        self.status = "ready"
                        self.status_message = "Waiting for consumer"
                else:
                    self.status = "connected"
                    self.status_message = "Stream active"

        # Process ended
        if not self._stop_event.is_set():
            self.status = "disconnected"
            self.status_message = "Process exited unexpectedly"

        self.process = None

    def _parse_stats(self, line: str):
        """Parse FFmpeg progress line"""
        patterns = {
            "bitrate": r"bitrate=\s*([\d\.]+\s*\w+/s)",
            "speed": r"speed=\s*([\d\.]+x)",
            "frame": r"frame=\s*(\d+)",
            "fps": r"fps=\s*([\d\.]+)",
            "drop": r"drop=\s*(\d+)",
            "dup": r"dup=\s*(\d+)"
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
                except:
                    pass

        # Track last time we saw any stats activity
        if "bitrate=" in line:
            self.last_data_time = time.time()


class UDPFeedMixer:
    """Forward either live or slate UDP feed to the output relay port.

    Outputs always read from the mixed port. While live packets are arriving,
    live feed is forwarded; otherwise slate feed keeps outputs alive.
    """

    def __init__(self, stream_id: int, live_port: int, slate_port: int, mixed_port: int):
        self.stream_id = stream_id
        self.live_port = live_port
        self.slate_port = slate_port
        self.mixed_port = mixed_port
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.live_active = False
        self.last_live_time = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _open_rx(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(False)
        sock.bind(("0.0.0.0", port))
        return sock

    def _run(self):
        try:
            live_sock = self._open_rx(self.live_port)
            slate_sock = self._open_rx(self.slate_port)
            out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            out_addr = ("127.0.0.1", self.mixed_port)
        except Exception as e:
            print(f"[MIXER {self.stream_id}] Failed to create sockets: {e}")
            return

        while not self._stop_event.is_set():
            try:
                readable, _, _ = select.select([live_sock, slate_sock], [], [], 0.05)
            except Exception:
                continue

            now = time.time()
            # Timeout live selection after 1 second of silence
            if self.live_active and now - self.last_live_time > 1.0:
                self.live_active = False
                print(f"[MIXER {self.stream_id}] Live lost, switching to slate")

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
                    try:
                        out_sock.sendto(data, out_addr)
                    except Exception:
                        pass
                elif sock is slate_sock:
                    if not self.live_active:
                        try:
                            out_sock.sendto(data, out_addr)
                        except Exception:
                            pass

        try:
            live_sock.close()
            slate_sock.close()
            out_sock.close()
        except Exception:
            pass


class UDPSplitter:
    """Duplicate the mixed feed to each output's own UDP port.

    Multiple outputs of the same input cannot all bind the same UDP port,
    so this splitter reads the mixed feed once and forwards a copy to every
    registered output port.
    """

    def __init__(self, stream_id: int, mixed_port: int):
        self.stream_id = stream_id
        self.mixed_port = mixed_port
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.outputs: Dict[int, int] = {}  # output_id -> out_port

    def _output_port(self, output_id: int) -> int:
        return 33000 + output_id

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def register(self, output_id: int) -> int:
        with self._lock:
            port = self._output_port(output_id)
            self.outputs[output_id] = port
            return port

    def unregister(self, output_id: int):
        with self._lock:
            self.outputs.pop(output_id, None)

    def _run(self):
        try:
            in_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            in_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            in_sock.setblocking(False)
            in_sock.bind(("0.0.0.0", self.mixed_port))
            out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except Exception as e:
            print(f"[SPLITTER {self.stream_id}] Failed to create sockets: {e}")
            return

        while not self._stop_event.is_set():
            try:
                readable, _, _ = select.select([in_sock], [], [], 0.05)
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

        try:
            in_sock.close()
            out_sock.close()
        except Exception:
            pass


class StreamManager:
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = os.getenv("DATA_DIR")
        if data_dir is None:
            # Default to project_root/data for local development
            data_dir = Path(__file__).resolve().parent.parent / "data"
        self.data_dir = str(data_dir)
        self.thumbnails_dir = os.path.join(self.data_dir, "thumbnails")
        self.slates_dir = os.path.join(self.data_dir, "slates")
        os.makedirs(self.thumbnails_dir, exist_ok=True)
        os.makedirs(self.slates_dir, exist_ok=True)

        self.input_processes: Dict[int, FFmpegProcess] = {}
        self.output_processes: Dict[int, FFmpegProcess] = {}
        self.slate_processes: Dict[int, FFmpegProcess] = {}
        self.mixers: Dict[int, UDPFeedMixer] = {}
        self.splitters: Dict[int, UDPSplitter] = {}
        self.input_configs: Dict[int, dict] = {}   # store params for auto-restart
        self.output_configs: Dict[int, dict] = {}  # store params for auto-restart
        self._lock = threading.Lock()

        # Start background monitor
        self._monitor_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._monitor_thread.start()

    # Internal UDP port layout (per input stream id)
    # 30000+id : live feed from SRT input
    # 31000+id : slate feed
    # 32000+id : mixed feed read by outputs
    def _live_port(self, stream_id: int) -> int:
        return 30000 + stream_id

    def _slate_port(self, stream_id: int) -> int:
        return 31000 + stream_id

    def _mixed_port(self, stream_id: int) -> int:
        return 32000 + stream_id

    def _slate_image_path(self, stream_id: int) -> str:
        return os.path.join(self.slates_dir, f"input_{stream_id}.jpg")

    def _thumbnail_path(self, stream_id: int) -> str:
        return os.path.join(self.thumbnails_dir, f"input_{stream_id}.jpg")

    @staticmethod
    def _find_font() -> Optional[str]:
        candidates = [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return None

    def build_input_cmd(self, stream_id: int, srt_url: str) -> list:
        """Build FFmpeg command for input stream (passthrough + thumbnail)"""
        thumbnail_path = self._thumbnail_path(stream_id)
        live_addr = f"127.0.0.1:{self._live_port(stream_id)}"
        return [
            "ffmpeg", "-y", "-fflags", "nobuffer", "-flags", "low_delay",
            "-i", srt_url,
            "-c", "copy", "-f", "mpegts", f"udp://{live_addr}?pkt_size=1316",
            "-map", "0:v", "-vf", "fps=1/3,scale=320:-1",
            "-update", "1", "-q:v", "2", thumbnail_path
        ]

    def build_slate_cmd(self, stream_id: int) -> list:
        """Build FFmpeg command that generates a placeholder/slate feed."""
        slate_port = self._slate_port(stream_id)
        thumbnail_path = self._thumbnail_path(stream_id)
        slate_image = self._slate_image_path(stream_id)

        # 1280x720 30fps slate with silent stereo audio
        if os.path.exists(slate_image):
            video_input = ["-re", "-loop", "1", "-framerate", "30", "-i", slate_image]
            video_filter = "format=yuv420p,scale=1280:720"
        else:
            # Default: plain black background. User can upload a custom image with text.
            video_input = ["-re", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30"]
            video_filter = "format=yuv420p"

        return [
            "ffmpeg", "-y",
            *video_input,
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", video_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-r", "30", "-b:v", "2000k",
            "-c:a", "aac", "-b:a", "128k",
            "-f", "mpegts", f"udp://127.0.0.1:{slate_port}?pkt_size=1316",
            "-map", "0:v", "-vf", "fps=1/3,scale=320:-1",
            "-update", "1", "-q:v", "2", thumbnail_path
        ]

    def build_output_cmd(self, stream_id: int, output_id: int, srt_url: str) -> list:
        """Build FFmpeg command for output stream"""
        out_addr = f"127.0.0.1:{33000 + output_id}"
        return [
            "ffmpeg", "-y", "-fflags", "nobuffer", "-flags", "low_delay",
            "-i", f"udp://{out_addr}?fifo_size=1000000&overrun_nonfatal=1",
            "-c", "copy", "-f", "mpegts", srt_url
        ]

    def _ensure_mixer(self, stream_id: int):
        if stream_id not in self.mixers or not self.mixers[stream_id]._thread:
            mixer = UDPFeedMixer(
                stream_id,
                self._live_port(stream_id),
                self._slate_port(stream_id),
                self._mixed_port(stream_id)
            )
            mixer.start()
            self.mixers[stream_id] = mixer

    def _ensure_splitter(self, stream_id: int):
        if stream_id not in self.splitters or not self.splitters[stream_id]._thread:
            splitter = UDPSplitter(stream_id, self._mixed_port(stream_id))
            splitter.start()
            self.splitters[stream_id] = splitter

    def _start_slate(self, stream_id: int) -> bool:
        if stream_id in self.slate_processes:
            proc = self.slate_processes[stream_id]
            if proc.process and proc.process.poll() is None:
                return True
            del self.slate_processes[stream_id]

        cmd = self.build_slate_cmd(stream_id)
        proc = FFmpegProcess(stream_id, cmd, is_input=False)
        if proc.start():
            self.slate_processes[stream_id] = proc
            return True
        return False

    def _stop_slate(self, stream_id: int):
        if stream_id in self.slate_processes:
            self.slate_processes[stream_id].stop()
            del self.slate_processes[stream_id]
            print(f"[SLATE] Stopped slate for input {stream_id}")

    def start_input(self, stream_id: int, srt_url: str) -> bool:
        with self._lock:
            self._ensure_mixer(stream_id)

            if stream_id in self.input_processes:
                proc = self.input_processes[stream_id]
                if proc.process and proc.process.poll() is None:
                    return True
                else:
                    del self.input_processes[stream_id]

            cmd = self.build_input_cmd(stream_id, srt_url)
            proc = FFmpegProcess(stream_id, cmd, is_input=True)
            if proc.start():
                self.input_processes[stream_id] = proc
                # Remember config for auto-restart on disconnect
                self.input_configs[stream_id] = {
                    "srt_url": srt_url,
                    "restart_attempts": 0
                }
                return True
            return False

    def stop_input(self, stream_id: int):
        with self._lock:
            if stream_id in self.input_processes:
                self.input_processes[stream_id].stop()
                del self.input_processes[stream_id]
            self._stop_slate(stream_id)
            if stream_id in self.mixers:
                self.mixers[stream_id].stop()
                del self.mixers[stream_id]
            if stream_id in self.splitters:
                self.splitters[stream_id].stop()
                del self.splitters[stream_id]
            self.input_configs.pop(stream_id, None)

    def start_output(self, stream_id: int, output_id: int, srt_url: str) -> bool:
        with self._lock:
            self._ensure_mixer(stream_id)
            self._ensure_splitter(stream_id)
            splitter = self.splitters[stream_id]
            splitter.register(output_id)

            if output_id in self.output_processes:
                proc = self.output_processes[output_id]
                if proc.process and proc.process.poll() is None:
                    return True
                else:
                    del self.output_processes[output_id]

            cmd = self.build_output_cmd(stream_id, output_id, srt_url)
            proc = FFmpegProcess(output_id, cmd, is_input=False)
            if proc.start():
                self.output_processes[output_id] = proc
                # Remember config for auto-restart on disconnect
                self.output_configs[output_id] = {
                    "stream_id": stream_id,
                    "srt_url": srt_url,
                    "restart_attempts": 0
                }
                return True
            splitter.unregister(output_id)
            return False

    def stop_output(self, output_id: int):
        with self._lock:
            if output_id in self.output_processes:
                self.output_processes[output_id].stop()
                del self.output_processes[output_id]
            self.output_configs.pop(output_id, None)
            for splitter in self.splitters.values():
                if output_id in splitter.outputs:
                    splitter.unregister(output_id)
                    break

    def get_input_status(self, stream_id: int) -> dict:
        with self._lock:
            proc = self.input_processes.get(stream_id)
            slate_proc = self.slate_processes.get(stream_id)
            slate_alive = slate_proc and slate_proc.process and slate_proc.process.poll() is None

            if proc and proc.process and proc.process.poll() is None:
                # If source is not actually connected but slate is feeding outputs,
                # report the input as being in slate mode.
                if proc.status != "connected" and slate_alive:
                    return {
                        "status": "slate",
                        "message": "No signal - slate active",
                        "stats": slate_proc.stats,
                        "uptime": (datetime.now() - slate_proc.start_time).total_seconds() if slate_proc.start_time else 0
                    }
                return {
                    "status": proc.status,
                    "message": proc.status_message,
                    "stats": proc.stats,
                    "uptime": (datetime.now() - proc.start_time).total_seconds() if proc.start_time else 0
                }
            if slate_alive:
                return {
                    "status": "slate",
                    "message": "No signal - slate active",
                    "stats": slate_proc.stats,
                    "uptime": (datetime.now() - slate_proc.start_time).total_seconds() if slate_proc.start_time else 0
                }
            return {"status": "disconnected", "message": "Not running", "stats": {}}

    def get_output_status(self, output_id: int) -> dict:
        with self._lock:
            if output_id not in self.output_processes:
                return {"status": "disconnected", "message": "Not running", "stats": {}}
            proc = self.output_processes[output_id]
            return {
                "status": proc.status,
                "message": proc.status_message,
                "stats": proc.stats,
                "uptime": (datetime.now() - proc.start_time).total_seconds() if proc.start_time else 0
            }

    def _health_check_loop(self):
        """Background health monitoring and auto-restart"""
        while True:
            time.sleep(5)
            to_restart = []
            with self._lock:
                # Check inputs
                for sid, proc in list(self.input_processes.items()):
                    process_exited = False
                    if proc.process and proc.process.poll() is not None:
                        proc.status = "disconnected"
                        proc.status_message = "Connection lost"
                        proc.process = None
                        process_exited = True
                    elif proc.process is None and sid in self.input_configs:
                        process_exited = True
                    elif proc.process:
                        if proc.status == "connected" and time.time() - proc.last_activity > 30:
                            proc.status = "warning"
                            proc.status_message = "No data received"

                    if process_exited and sid in self.input_configs:
                        to_restart.append(("input", sid))

                    # Keep slate running while source is not actually connected
                    if sid in self.input_configs and proc.status != "connected":
                        self._ensure_mixer(sid)
                        if self._start_slate(sid):
                            print(f"[SLATE] Slate active for input {sid}")
                    elif proc.status == "connected":
                        self._stop_slate(sid)

                # Check outputs
                for oid, proc in list(self.output_processes.items()):
                    process_exited = False
                    if proc.process and proc.process.poll() is not None:
                        proc.status = "disconnected"
                        proc.status_message = "Connection lost"
                        proc.process = None
                        process_exited = True
                    elif proc.process is None and oid in self.output_configs:
                        process_exited = True
                    elif proc.process:
                        if proc.status == "connected" and time.time() - proc.last_activity > 30:
                            proc.status = "warning"
                            proc.status_message = "No data received"

                    if process_exited and oid in self.output_configs:
                        to_restart.append(("output", oid))

            # Restart outside the lock to avoid deadlocks
            for kind, id_ in to_restart:
                try:
                    if kind == "input":
                        cfg = self.input_configs.get(id_)
                        if cfg:
                            cfg["restart_attempts"] += 1
                            if cfg["restart_attempts"] > 5:
                                print(f"[HEALTH] Giving up on input {id_} after 5 restart attempts")
                                self.input_configs.pop(id_, None)
                                continue
                            print(f"[HEALTH] Auto-restarting input {id_} (attempt {cfg['restart_attempts']})")
                            self.start_input(id_, cfg["srt_url"])
                    else:
                        cfg = self.output_configs.get(id_)
                        if cfg:
                            cfg["restart_attempts"] += 1
                            if cfg["restart_attempts"] > 5:
                                print(f"[HEALTH] Giving up on output {id_} after 5 restart attempts")
                                self.output_configs.pop(id_, None)
                                continue
                            print(f"[HEALTH] Auto-restarting output {id_} (attempt {cfg['restart_attempts']})")
                            self.start_output(cfg["stream_id"], id_, cfg["srt_url"])
                except Exception as e:
                    print(f"[HEALTH] Failed to restart {kind} {id_}: {e}")

# Global manager instance
stream_manager = StreamManager()
