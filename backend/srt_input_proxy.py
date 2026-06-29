"""SRT input proxy using srt-live-transmit.

For SRT inputs we can't read deep socket statistics from FFmpeg.
This proxy runs the SRT reference tool `srt-live-transmit`, forwards the
MPEG-TS payload to the local UDP port that FFmpeg already consumes, and
parses the JSON statistics output.
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


SRT_LIVE_TRANSMIT = Path(__file__).resolve().parent / "tools" / "srt-live-transmit.exe"


class SrtInputProxy:
    """Receive an SRT stream and forward it to a local UDP port.

    Parses JSON statistics emitted by srt-live-transmit and exposes them
    via ``self.stats``.
    """

    def __init__(self, stream_id: int, srt_url: str, live_port: int):
        self.stream_id = stream_id
        self.srt_url = srt_url
        self.live_port = live_port
        self.process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self.status = "disconnected"
        self.status_message = "Not started"
        self.start_time: Optional[datetime] = None
        self.last_activity = 0.0
        self.restart_attempts = 0

        # Friendly statistics for the UI / API
        self.stats: Dict[str, any] = {
            "state": "Disconnected",
            "peer_version": "",
            "peer_endpoint": "",
            "local_endpoint": self._local_endpoint(srt_url),
            "peer_address": "",
            "peer_port": "",
            "local_address": "",
            "local_port": "",
            "encryption": "On" if self._has_passphrase(srt_url) else "Off",
            "authentication": "Off",
            "reconnections": 0,
            "lost_packets": 0,
            "recovered_packets": 0,
            "skipped_packets": 0,
            "sent_acks": None,
            "sent_naks": None,
            "link_bandwidth_kbps": 0,
            "rtt_ms": 0,
            "local_buffer_ms": 0,
            "latency_ms": 0,
            "recv_rate_mbps": 0.0,
        }

    @staticmethod
    def _has_passphrase(url: str) -> bool:
        try:
            return bool(parse_qs(urlparse(url).query).get("passphrase"))
        except Exception:
            return False

    @staticmethod
    def _local_endpoint(url: str) -> str:
        try:
            parsed = urlparse(url)
            host = parsed.hostname or "0.0.0.0"
            return f"{host}:{parsed.port}" if parsed.port else host
        except Exception:
            return ""

    def _build_cmd(self) -> list:
        """Build srt-live-transmit command line."""
        binary = str(SRT_LIVE_TRANSMIT)
        if not os.path.exists(binary):
            raise FileNotFoundError(f"SRT tool not found: {binary}")

        # Normalize URL so 0.0.0.0 becomes empty host (listener) if needed.
        src_url = self.srt_url
        parsed = urlparse(src_url)
        if parsed.scheme.lower() == "srt" and parsed.hostname == "0.0.0.0":
            netloc = f":{parsed.port}"
            src_url = urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))

        dst_url = f"udp://127.0.0.1:{self.live_port}?pkt_size=1316"

        # Stats frequency in packets. At ~3 Mbps / 1316 bytes ~= 285 pkt/s,
        # 250 gives roughly one report per second. We update UI every second.
        stats_freq = 250

        return [
            binary,
            "-a", "yes",                 # auto-reconnect
            "-buffering", "1",           # minimal internal buffering
            "-chunk", "1316",
            "-s", str(stats_freq),       # stats report every N packets
            "-statsout", "-",            # stats to stdout
            "-statspf", "json",
            "-f",                        # full (total) counters
            "-v",                        # verbose -> connection messages on stderr
            src_url,
            dst_url,
        ]

    def start(self) -> bool:
        if self.process and self.process.poll() is None:
            return True

        try:
            cmd = self._build_cmd()
        except Exception as e:
            self.status = "error"
            self.status_message = str(e)
            return False

        self._stop_event.clear()
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            self.status = "error"
            self.status_message = f"Failed to start SRT proxy: {e}"
            return False

        self.start_time = datetime.now()
        self.status = "connecting"
        self.status_message = "Waiting for SRT connection"
        self.restart_attempts = 0
        self.last_activity = time.time()

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop_event.set()
        proc = self.process
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.process = None
        self.status = "disconnected"
        self.status_message = "Stopped"
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _update_stats(self, raw: dict):
        """Translate srt-live-transmit JSON counters into friendly fields."""
        with self._lock:
            self.last_activity = time.time()
            if self.status != "connected":
                self.status = "connected"
                self.status_message = "SRT connected"

            recv = raw.get("recv") or {}
            link = raw.get("link") or {}

            pkt_recv = int(recv.get("packets", 0))
            pkt_recv_unique = int(recv.get("packetsUnique", 0))
            pkt_rcv_loss = int(recv.get("packetsLost", 0))
            pkt_rcv_retrans = int(recv.get("packetsRetransmitted", 0))
            pkt_rcv_drop = int(recv.get("packetsDropped", 0))

            ms_rcv_buf = float(recv.get("msBuf", 0))
            ms_rcv_tsbpd_delay = float(recv.get("msTsbPdDelay", 0))
            mbps_recv_rate = float(recv.get("mbitRate", 0))

            ms_rtt = float(link.get("rtt", 0))
            mbps_bandwidth = float(link.get("bandwidth", 0))

            self.stats.update({
                "state": "Active",
                "lost_packets": pkt_rcv_loss,
                "recovered_packets": pkt_rcv_retrans,
                "skipped_packets": pkt_rcv_drop,
                "link_bandwidth_kbps": int(mbps_bandwidth * 1000),
                "recv_rate_mbps": round(mbps_recv_rate, 3),
                "rtt_ms": int(ms_rtt),
                "local_buffer_ms": int(ms_rcv_buf),
                "latency_ms": int(ms_rcv_tsbpd_delay),
                "reconnections": self.restart_attempts,
                "raw_packets_received": pkt_recv,
                "raw_packets_unique": pkt_recv_unique,
            })

    def _parse_stderr(self, line: str):
        """Parse verbose stderr for connection info not present in JSON stats."""
        # Newer srt-live-transmit builds just say "Accepted SRT source connection".
        # Older builds may include the peer address; keep the regex as a fallback.
        m = re.search(r"(?:Accepted SRT .* from|Connected to)\s+([\d\.]+):(\d+)", line)
        if m:
            with self._lock:
                self.stats["peer_address"] = m.group(1)
                self.stats["peer_port"] = int(m.group(2))
                self.stats["peer_endpoint"] = f"{m.group(1)}:{m.group(2)}"

    def _run(self):
        proc = self.process
        if not proc or not proc.stdout or not proc.stderr:
            return

        # Spin up a stderr reader so verbose logs don't block.
        def stderr_reader():
            try:
                for line in proc.stderr:
                    line = line.strip()
                    if line:
                        self._parse_stderr(line)
            except Exception:
                pass

        err_thread = threading.Thread(target=stderr_reader, daemon=True)
        err_thread.start()

        json_buf = ""
        in_json = False
        try:
            for line in proc.stdout:
                if self._stop_event.is_set():
                    break

                line = line.strip()
                if not line:
                    continue

                # JSON stats line(s) handling. srt-live-transmit prints compact JSON.
                if line.startswith("{"):
                    in_json = True
                    json_buf = line
                elif in_json:
                    json_buf += line
                else:
                    continue

                if in_json and json_buf.count("{") == json_buf.count("}"):
                    in_json = False
                    try:
                        data = json.loads(json_buf)
                        self._update_stats(data)
                    except Exception:
                        pass
                    json_buf = ""
        except Exception as e:
            with self._lock:
                if not self._stop_event.is_set():
                    self.status = "error"
                    self.status_message = f"SRT proxy reader failed: {e}"

        err_thread.join(timeout=1)

        with self._lock:
            if not self._stop_event.is_set() and self.status != "error":
                self.status = "disconnected"
                self.status_message = "SRT proxy exited"

    def get_stats(self) -> dict:
        with self._lock:
            stats = dict(self.stats)
            stats["proxy_status"] = self.status
            stats["proxy_message"] = self.status_message
            stats["proxy_uptime"] = (
                (datetime.now() - self.start_time).total_seconds()
                if self.start_time else 0
            )
            return stats

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None
