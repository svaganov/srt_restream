# AGENTS.md — srt_restream

> This file is intended for AI coding agents working on this repository.
> It describes the project as it currently exists, not as it is planned to become.

## Project overview

`srt_restream` is a web application for receiving SRT (Secure Reliable Transport) streams and redistributing them to multiple consumers via SRT with full passthrough (no transcoding).

### Architecture

```
Source (OBS/etc) → Input FFmpeg (SRT→UDP) → UDP relay on localhost → Output FFmpeg(s) (UDP→SRT) → Consumers
                                   ↓
                          Web Dashboard (FastAPI + SQLite)
```

Each input and output runs as an independent FFmpeg process so that a failure in one consumer does not affect others.

### Technology stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2.x, Pydantic 2.x |
| Frontend | Vanilla JavaScript, CSS3, WebSocket |
| Database | SQLite (file-based, default `data/restreamer.db`) |
| Streaming | FFmpeg with SRT protocol |
| Auth | JWT (python-jose), bcrypt (passlib) |
| Container | Docker, Docker Compose |

## Repository state

- Branch: `main`
- Remote: `origin` → `https://github.com/svaganov/srt_restream.git`
- Tracked files: backend, frontend, Docker files, README.md, LICENSE, `.vscode/`, AGENTS.md
- Untracked files: none (excluding runtime data and `.env`)

## Project structure

```
srt_restream/
├── backend/
│   ├── main.py              # FastAPI entry point, static files, startup
│   ├── api.py               # REST API endpoints & WebSocket
│   ├── auth.py              # JWT authentication & password hashing
│   ├── models.py            # SQLAlchemy database models
│   ├── stream_manager.py    # FFmpeg process management & stats parsing
│   └── requirements.txt     # Python dependencies
├── frontend/
│   ├── templates/           # Jinja-compatible HTML pages
│   │   ├── index.html       # Dashboard
│   │   └── login.html       # Login page
│   └── static/
│       ├── css/style.css    # Dark broadcast theme
│       └── js/app.js        # Dashboard logic & WebSocket client
├── data/                    # SQLite DB & thumbnails (Docker volume, gitignored)
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── .vscode/                 # VS Code extensions, launch config, settings
├── LICENSE                  # MIT
└── README.md
```

## Build, run and test commands

### Docker (recommended)

```bash
cp .env.example .env
# Edit SECRET_KEY in .env
docker-compose up --build
```

The dashboard is available at `http://localhost:8080/login`.

Default credentials (intentionally left as-is; will be changed later):

- Username: `admin`
- Password: `admin`

### Local development (without Docker)

Requires FFmpeg installed on the system.

```bash
cd backend
python -m venv venv
# Windows: venv\Scripts\activate
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

Paths to static files, templates, and the database are resolved relative to the project root, so the application works whether you run it from the project root or from the `backend/` directory.

### Health check

```bash
curl -f http://localhost:8080/api/stats
```

## Code style guidelines

- Python: follow PEP 8, use type hints where reasonable.
- Backend imports: prefer absolute imports within the `backend/` package (e.g. `from models import ...`, `from auth import ...`).
- Frontend: vanilla JS, no build step; keep functions modular and event-driven.
- Configuration: read runtime values from environment variables with sensible defaults; document them in `.env.example`.
- Do not commit secrets, credentials, `.env` files, or runtime data under `data/`.

## Testing instructions

No automated test suite exists yet. Manual testing workflow:

1. Start the application (Docker or local).
2. Log in with default credentials.
3. Create an input stream, e.g. `srt://0.0.0.0:5000?mode=listener&latency=200`.
4. Start the input stream.
5. Publish an SRT stream to that endpoint (e.g. from OBS or FFmpeg).
6. Add one or more output streams and start them.
7. Verify consumers receive the stream and the dashboard stats update via WebSocket.

For low-latency verification, launch the consumer with low-latency flags, because `ffplay` buffers network streams by default and can add several seconds of perceived delay:

```bash
ffplay -fflags nobuffer -flags low_delay "srt://127.0.0.1:6002?mode=caller&latency=200"
```

## Security considerations

- JWT secret key must be changed in production via `SECRET_KEY` env var.
- Default admin credentials are currently hardcoded and must be changed before any production use.
- WebSocket connections require a valid JWT token passed as a query parameter (`?token=...`).
- The application uses Docker `network_mode: host` so SRT UDP ports are bound directly on the host.
- Do not commit API keys, certificates, passwords, or `.env` files.

## Notes for future agents

- Before making changes, verify the current repository state with `git status` and `git log`.
- Update this file if the project structure, stack, or build commands change.
- FFmpeg commands live in `backend/stream_manager.py`. Any change to SRT options, UDP relay, or thumbnail capture should be made there.
- Thumbnails are generated by the input FFmpeg process itself (`-vf fps=1/3,scale=320:-1 -update 1`) and saved to `data/thumbnails/`.
- The frontend relies on `/api/inputs` plus `/api/outputs/{input_id}` to render the dashboard.
