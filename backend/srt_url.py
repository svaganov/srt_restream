"""SRT URL parsing and validation."""
import os
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, field_validator


DEFAULT_LISTENER_PORT_RANGE = "5000-5999"
INTERNAL_UDP_PORT_RANGE = "40000-49999"


def _parse_range(rng: str):
    start, end = rng.split("-", 1)
    return int(start), int(end)


def _in_range(port: int, rng: str) -> bool:
    start, end = _parse_range(rng)
    return start <= port <= end


def _is_loopback(host: str) -> bool:
    host = host.lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.startswith("127."):
        return True
    return False


class SrtUrl(BaseModel):
    """Strict SRT URL model.

    - Only `srt://` scheme is allowed.
    - Host and port are mandatory.
    - `mode` is required in the query string and must be `caller` or `listener`.
    - Listener ports must be inside the configured public listener range.
    - Caller URLs are blocked from loopback and internal UDP ports.
    - Passphrase is NOT accepted inside the URL; use the separate passphrase field.
    """

    raw: str
    scheme: str
    host: str
    port: int
    mode: str
    query: dict[str, list[str]]
    allowed_listener_range: str
    internal_udp_range: str

    @field_validator("raw")
    @classmethod
    def _validate_scheme(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme.lower() != "srt":
            raise ValueError("URL scheme must be srt://")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("SRT URL must not contain userinfo")
        if parsed.path and parsed.path != "/":
            raise ValueError("SRT URL must not contain a path")
        if parsed.fragment:
            raise ValueError("SRT URL must not contain a fragment")
        if not parsed.hostname:
            raise ValueError("SRT URL must contain a host")
        if not parsed.port:
            raise ValueError("SRT URL must contain a port")
        if parsed.hostname.lower() in ("localhost",):
            raise ValueError("Use a numeric IP or public hostname instead of localhost")
        return v

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        v = v.lower()
        if v not in ("caller", "listener"):
            raise ValueError("mode must be caller or listener")
        return v

    @classmethod
    def parse(cls, url: str) -> "SrtUrl":
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        modes = query.get("mode", [])
        if not modes:
            raise ValueError("SRT URL is missing mode=caller|listener")
        if len(modes) > 1:
            raise ValueError("SRT URL must contain only one mode value")
        mode = modes[0]

        if "passphrase" in query:
            raise ValueError("Passphrase must be provided via the separate passphrase field, not in the URL")

        listener_range = os.getenv("SRT_LISTENER_PORT_RANGE", DEFAULT_LISTENER_PORT_RANGE)
        internal_range = os.getenv("INTERNAL_UDP_PORT_RANGE", INTERNAL_UDP_PORT_RANGE)

        host = parsed.hostname.lower()
        port = parsed.port

        obj = cls(
            raw=url,
            scheme=parsed.scheme,
            host=host,
            port=port,
            mode=mode,
            query=query,
            allowed_listener_range=listener_range,
            internal_udp_range=internal_range,
        )

        if obj.mode == "listener":
            if not _in_range(port, listener_range):
                raise ValueError(
                    f"Listener port {port} is outside the allowed range {listener_range}"
                )
        else:
            if _is_loopback(host):
                raise ValueError("Caller URL must not point to loopback addresses")
            if _in_range(port, internal_range):
                raise ValueError(
                    f"Caller port {port} conflicts with the internal UDP range {internal_range}"
                )

        return obj

    def with_passphrase(self, passphrase: str) -> str:
        """Return the URL with the passphrase appended as a query parameter."""
        sep = "&" if "?" in self.raw else "?"
        return f"{self.raw}{sep}passphrase={passphrase}"

    @property
    def has_mode_conflict(self, explicit_mode: str | None) -> bool:
        """True if an explicitly provided mode does not match the URL mode."""
        if explicit_mode is None:
            return False
        return explicit_mode.lower() != self.mode
