# SRT Restreamer

Professional SRT stream relay and management web application. Receive SRT streams from sources and independently redistribute them to multiple consumers via SRT protocol with full passthrough (no transcoding).

## Features

- **Web Dashboard** — Manage all input and output streams through a modern dark-themed web interface
- **SRT Input/Output** — Support both `listener` and `caller` modes; mode is derived from the URL
- **Full Passthrough** — All tracks are relayed (`-map 0 -c copy`); audio-only and multi-audio inputs work
- **Isolated Consumers** — Each output runs as an independent FFmpeg process
- **Real-time Stats** — Live bitrate, FPS, speed monitoring via WebSocket
- **Thumbnail Preview** — Automatic keyframe capture in a separate best-effort process
- **Slate Fallback** — Real-time paced placeholder feed; only for single-track H.264/AAC inputs
- **Session Authentication** — Server-side opaque sessions (Argon2 passwords), CSRF protection, secure cookies
- **Encrypted Secrets** — SRT passphrases stored encrypted (Fernet), never returned by the API
- **SQLite Database** — Persistent storage with Alembic migrations, WAL mode
- **Docker Ready** — Hardened single-command deployment with Docker Compose

## Architecture

```
┌─────────────┐     SRT      ┌─────────────────┐     UDP      ┌─────────────────┐
│   Source    │ ───────────→ │  Input FFmpeg   │ ───────────→ │  Output FFmpeg  │ ──→ Consumer #1 (SRT)
│  (OBS/etc)  │              │   (passthrough) │  loopback    │   (passthrough) │
└─────────────┘              └─────────────────┘              └─────────────────┘ ──→ Consumer #N (SRT)
                                      │
                                      ↓
                              ┌───────────────┐
                              │  Web Dashboard │
                              │  (FastAPI)     │
                              └───────────────┘
```

All internal UDP sockets bind to `127.0.0.1` only — the media plane is never
reachable from outside the host.

## Quick Start

### One command (local development)

```bash
bash start.sh        # Windows: start.bat
```

On first run this creates the virtualenv, installs dependencies, generates
`.env` with secrets, creates the admin user (prints the password once) and
starts the app at `http://localhost:8080/login`. Subsequent runs reuse
everything. To set a fixed admin password, export it first:
`ADMIN_PASSWORD='...' bash start.sh`.

### 1. Clone & Configure (manual)

```bash
git clone https://github.com/svaganov/srt_restream.git
cd srt_restream
cp .env.example .env
# Generate secrets:
python -c "import secrets; print(secrets.token_urlsafe(32))"                      # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # SECRETS_KEY
# Put both into .env. Set APP_BASE_URL to your public https URL.
```

### 2. Create the first admin (one-time, interactive)

```bash
python backend/bootstrap_admin.py
# or: ADMIN_PASSWORD='...' python backend/bootstrap_admin.py --username admin
```

The application **refuses to start** without configured secrets and at least
one admin user. There is no default `admin/admin` account.

### 3. Run with Docker

```bash
docker-compose up --build
```

### 4. Open Dashboard

Navigate to your configured `APP_BASE_URL` (e.g. `http://localhost:8080/login`).

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SECRET_KEY` | yes | Strong random value; startup fails without it |
| `SECRETS_KEY` | yes | Fernet key for encrypting passphrases |
| `APP_BASE_URL` | yes (prod) | Public base URL used for Origin/CSRF checks |
| `DATABASE_URL` | no | Default `sqlite:///data/restreamer.db` |
| `DATA_DIR` | no | Default `/app/data` (Docker) or `./data` |
| `SRT_LISTENER_PORT_RANGE` | no | Allowed listener ports, default `5000-5999` |
| `INTERNAL_PORT_START` / `INTERNAL_PORT_END` | no | Loopback media-plane ports, default `40000-49999` |
| `SESSION_COOKIE_SECURE` | no | `true` in production; `false` only for local HTTP dev |

## SRT URL Format

Only `srt://` URLs are accepted. `host`, `port` and `mode` are mandatory.
Passphrases are provided via the separate write-only API field — never in the URL.

### Input (Listener — wait for source to connect)
```
srt://0.0.0.0:5000?mode=listener&latency=200&transtype=live
```

### Output (Caller — connect to remote consumer)
```
srt://remote.host:6001?mode=caller&latency=200&transtype=live
```

### Output (Listener — wait for consumer to connect)
```
srt://0.0.0.0:6002?mode=listener&latency=200&transtype=live
```

Listener ports must be inside `SRT_LISTENER_PORT_RANGE`.

## Security Model

- **Auth** — Server-side opaque sessions. Session token is a random 256-bit
  value stored in the DB as SHA-256 hash; cookie is `HttpOnly`, `Secure`,
  `SameSite=Strict`, 8-hour lifetime. Passwords are Argon2id hashes,
  minimum 12 characters.
- **CSRF/Origin** — Mutating REST requests require an `X-CSRF-Token` header
  (double-submit cookie) and a matching `Origin`/`Referer` against
  `APP_BASE_URL`. WebSocket connections validate session + origin.
- **Rate limiting** — Login is limited to 5 attempts per minute per IP.
- **SRT URL validation** — Only `srt://` with explicit host/port/mode;
  no other protocols (prevents SSRF / file access through FFmpeg).
- **Secrets** — Passphrases are encrypted at rest (Fernet), redacted from
  logs, and excluded from config exports.
- **Container** — Non-root user, `cap_drop: ALL`, `no-new-privileges`,
  read-only root filesystem, selective `COPY` + strict `.dockerignore`.

## Limitations

- **Passthrough only** — no transcoding; codecs are relayed as-is.
- **Slate** — real-time paced (`-re`) 720p30 H.264/AAC fallback. Seamless
  slate switching is only enabled for single-track H.264/AAC inputs; for
  other codec layouts the dashboard shows "slate unavailable".
- **SRT statistics** — the `srt-live-transmit` proxy was removed from the
  critical path; the SRT stats endpoint currently returns `available: false`.
- **Single worker** — run Uvicorn with one worker (default in the provided
  Docker image). Multi-process deployments are not supported.

## API Reference

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login` | Login; sets session + CSRF cookies |
| POST | `/api/auth/logout` | Invalidate current session |
| POST | `/api/auth/change-password` | Change password; revokes all sessions |
| GET | `/api/auth/me` | Session probe |

### Input Streams

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/inputs` | List all input streams with status |
| POST | `/api/inputs` | Create new input stream |
| PUT | `/api/inputs/{id}` | Update input stream |
| DELETE | `/api/inputs/{id}` | Delete input stream and all outputs |
| POST | `/api/inputs/{id}/start` | Mark input as desired (202, idempotent) |
| POST | `/api/inputs/{id}/stop` | Stop input (202, idempotent) |
| GET | `/api/inputs/{id}/thumbnail` | Get latest thumbnail (session cookie) |

### Output Streams

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/outputs/{input_id}` | List outputs for an input |
| POST | `/api/outputs` | Create new output stream |
| DELETE | `/api/outputs/{id}` | Delete output stream |
| POST | `/api/outputs/{id}/start` | Start output (202, idempotent) |
| POST | `/api/outputs/{id}/stop` | Stop output (202, idempotent) |

### Health & Stats

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health/live` | Liveness probe (unauthenticated) |
| GET | `/health/ready` | Readiness probe with DB check (unauthenticated) |
| GET | `/api/stats` | Current statistics for all streams |
| WS | `/api/ws` | WebSocket for live stats (session cookie) |

## Project Structure

```
srt_restream/
├── backend/
│   ├── main.py              # FastAPI application, lifespan, security middleware
│   ├── api.py               # REST API endpoints & WebSocket
│   ├── auth.py              # Sessions, Argon2, CSRF/Origin, rate limiting
│   ├── srt_url.py           # Strict SRT URL validation
│   ├── encryption.py        # Fernet encryption for passphrases
│   ├── models.py            # SQLAlchemy models, SQLite WAL
│   ├── bootstrap_admin.py   # One-time admin creation CLI
│   ├── stream_manager.py    # Single-flight FFmpeg supervisor
│   ├── alembic/             # Database migrations
│   └── tests/               # pytest suite
├── frontend/
│   ├── templates/           # index.html, login.html
│   └── static/              # css, js (DOM-safe, no inline handlers)
├── data/                    # SQLite DB & thumbnails (Docker volume)
├── Dockerfile               # Hardened non-root build
├── docker-compose.yml       # Compose with published SRT port range
└── README.md
```

## Development

```bash
cd backend
python -m venv ../.venv
source ../.venv/bin/activate  # Windows: ..\.venv\Scripts\activate
pip install -r requirements.txt -r ../requirements-dev.txt

# Bootstrap admin (one time)
python bootstrap_admin.py

# Run tests
pytest tests -q

# Run app
python main.py
```

## Technology Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11, FastAPI 0.140, SQLAlchemy 2, Alembic |
| Frontend | Vanilla JS (DOM-safe), CSS3, WebSocket |
| Database | SQLite (WAL mode) |
| Streaming | FFmpeg with SRT protocol |
| Container | Docker, Docker Compose |
| Auth | Opaque sessions, Argon2id, Fernet |

## License

MIT License — see LICENSE file for details.
