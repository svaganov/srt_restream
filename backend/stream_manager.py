"""Stream process manager using FFmpeg"""
import subprocess
import threading
import time
import os
import signal
import re
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

class StreamManager:
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = os.getenv("DATA_DIR")
        if data_dir is None:
            # Default to project_root/data for local development
            data_dir = Path(__file__).resolve().parent.parent / "data"
        self.data_dir = str(data_dir)
        self.thumbnails_dir = os.path.join(self.data_dir, "thumbnails")
        os.makedirs(self.thumbnails_dir, exist_ok=True)

        self.input_processes: Dict[int, FFmpegProcess] = {}
        self.output_processes: Dict[int, FFmpegProcess] = {}
        self.input_configs: Dict[int, dict] = {}   # store params for auto-restart
        self.output_configs: Dict[int, dict] = {}  # store params for auto-restart
        self._lock = threading.Lock()

        # Start background monitor
        self._monitor_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._monitor_thread.start()

    def build_input_cmd(self, stream_id: int, srt_url: str, udp_relay: str) -> list:
        """Build FFmpeg command for input stream (passthrough + thumbnail)"""
        thumbnail_path = os.path.join(self.thumbnails_dir, f"input_{stream_id}.jpg")
        return [
            "ffmpeg", "-y", "-fflags", "nobuffer", "-flags", "low_delay",
            "-i", srt_url,
            "-c", "copy", "-f", "mpegts", f"udp://{udp_relay}?pkt_size=1316",
            "-map", "0:v", "-vf", "fps=1/3,scale=320:-1",
            "-update", "1", "-q:v", "2", thumbnail_path
        ]

    def build_output_cmd(self, stream_id: int, srt_url: str, udp_source: str) -> list:
        """Build FFmpeg command for output stream"""
        return [
            "ffmpeg", "-y", "-fflags", "nobuffer", "-flags", "low_delay",
            "-i", f"udp://{udp_source}?fifo_size=1000000&overrun_nonfatal=1",
            "-c", "copy", "-f", "mpegts", srt_url
        ]


    def start_input(self, stream_id: int, srt_url: str, udp_relay: str) -> bool:
        with self._lock:
            if stream_id in self.input_processes:
                proc = self.input_processes[stream_id]
                if proc.process and proc.process.poll() is None:
                    return True
                else:
                    del self.input_processes[stream_id]

            cmd = self.build_input_cmd(stream_id, srt_url, udp_relay)
            proc = FFmpegProcess(stream_id, cmd, is_input=True)
            if proc.start():
                self.input_processes[stream_id] = proc
                # Remember config for auto-restart on disconnect
                self.input_configs[stream_id] = {
                    "srt_url": srt_url,
                    "udp_relay": udp_relay,
                    "restart_attempts": 0
                }
                return True
            return False

    def stop_input(self, stream_id: int):
        with self._lock:
            if stream_id in self.input_processes:
                self.input_processes[stream_id].stop()
                del self.input_processes[stream_id]
            self.input_configs.pop(stream_id, None)


    def start_output(self, stream_id: int, output_id: int, srt_url: str, udp_source: str) -> bool:
        with self._lock:
            if output_id in self.output_processes:
                proc = self.output_processes[output_id]
                if proc.process and proc.process.poll() is None:
                    return True
                else:
                    del self.output_processes[output_id]

            cmd = self.build_output_cmd(output_id, srt_url, udp_source)
            proc = FFmpegProcess(output_id, cmd, is_input=False)
            if proc.start():
                self.output_processes[output_id] = proc
                # Remember config for auto-restart on disconnect
                self.output_configs[output_id] = {
                    "stream_id": stream_id,
                    "srt_url": srt_url,
                    "udp_source": udp_source,
                    "restart_attempts": 0
                }
                return True
            return False

    def stop_output(self, output_id: int):
        with self._lock:
            if output_id in self.output_processes:
                self.output_processes[output_id].stop()
                del self.output_processes[output_id]
            self.output_configs.pop(output_id, None)

    def get_input_status(self, stream_id: int) -> dict:
        with self._lock:
            if stream_id not in self.input_processes:
                return {"status": "disconnected", "message": "Not running", "stats": {}}
            proc = self.input_processes[stream_id]
            return {
                "status": proc.status,
                "message": proc.status_message,
                "stats": proc.stats,
                "uptime": (datetime.now() - proc.start_time).total_seconds() if proc.start_time else 0
            }

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
                        # Monitor thread already cleaned up the process
                        process_exited = True
                    elif proc.process:
                        # Only mark warning if the stream was previously active and went silent
                        if proc.status == "connected" and time.time() - proc.last_activity > 30:
                            proc.status = "warning"
                            proc.status_message = "No data received"

                    if process_exited and sid in self.input_configs:
                        to_restart.append(("input", sid))

                # Check outputs
                for oid, proc in list(self.output_processes.items()):
                    process_exited = False
                    if proc.process and proc.process.poll() is not None:
                        proc.status = "disconnected"
                        proc.status_message = "Connection lost"
                        proc.process = None
                        process_exited = True
                    elif proc.process is None and oid in self.output_configs:
                        # Monitor thread already cleaned up the process
                        process_exited = True
                    elif proc.process:
                        # Only mark warning if the stream was previously active and went silent
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
                            self.start_input(id_, cfg["srt_url"], cfg["udp_relay"])
                    else:
                        cfg = self.output_configs.get(id_)
                        if cfg:
                            cfg["restart_attempts"] += 1
                            if cfg["restart_attempts"] > 5:
                                print(f"[HEALTH] Giving up on output {id_} after 5 restart attempts")
                                self.output_configs.pop(id_, None)
                                continue
                            print(f"[HEALTH] Auto-restarting output {id_} (attempt {cfg['restart_attempts']})")
                            self.start_output(cfg["stream_id"], id_, cfg["srt_url"], cfg["udp_source"])
                except Exception as e:
                    print(f"[HEALTH] Failed to restart {kind} {id_}: {e}")

# Global manager instance
stream_manager = StreamManager()
