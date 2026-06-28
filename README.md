# SRT Restreamer

Professional SRT stream relay and management web application. Receive SRT streams from sources and independently redistribute them to multiple consumers via SRT protocol with full passthrough (no transcoding).

## Features

- **Web Dashboard** — Manage all input and output streams through a modern dark-themed web interface
- **SRT Input/Output** — Support both `listener` and `caller` modes for any stream endpoint
- **Isolated Consumers** — Each output runs as an independent FFmpeg process. If one consumer disconnects, all others continue working
- **Real-time Stats** — Live bitrate, FPS, speed monitoring via WebSocket
- **Thumbnail Preview** — Automatic keyframe capture from input streams
- **JWT Authentication** — Secure login with bearer tokens
- **SQLite Database** — Persistent storage of stream configurations
- **Docker Ready** — Single-command deployment with Docker Compose

## Architecture

```
┌─────────────┐     SRT      ┌─────────────────┐     UDP      ┌─────────────────┐
│   Source    │ ───────────→ │  Input FFmpeg   │ ───────────→ │  Output FFmpeg  │ ──→ Consumer #1 (SRT)
│  (OBS/etc)  │              │   (passthrough) │  localhost   │   (passthrough) │
└─────────────┘              └─────────────────┘              └─────────────────┘ ──→ Consumer #N (SRT)
                                      │
                                      ↓
                              ┌───────────────┐
                              │  Web Dashboard │
                              │  (FastAPI)     │
                              └───────────────┘
```

## Quick Start

### 1. Clone & Configure

```bash
git clone https://github.com/svaganov/srt-restreamer.git
cd srt-restreamer
cp .env.example .env
# Edit .env and set SECRET_KEY
```

### 2. Run with Docker

```bash
docker-compose up --build
```

### 3. Open Dashboard

Navigate to `http://localhost:8080/login`

- **Username:** `admin`
- **Password:** `admin`

### 4. Add Your First Stream

1. Click **"Add Input Stream"**
2. **Name:** `Main Feed`
3. **SRT URL:** `srt://0.0.0.0:5000?mode=listener&latency=200`
4. Click **Start**
5. Add output destinations under the stream card

## Project Structure

```
srt-restreamer/
├── backend/
│   ├── main.py              # FastAPI application entry point
│   ├── api.py               # REST API endpoints & WebSocket
│   ├── auth.py              # JWT authentication & password hashing
│   ├── models.py            # SQLAlchemy database models
│   ├── stream_manager.py    # FFmpeg process management
│   └── requirements.txt     # Python dependencies
├── frontend/
│   ├── templates/
│   │   ├── index.html       # Dashboard page
│   │   └── login.html       # Login page
│   └── static/
│       ├── css/style.css    # Dark broadcast theme
│       └── js/app.js        # Frontend logic & WebSocket client
├── data/                    # SQLite DB & thumbnails (Docker volume)
├── Dockerfile               # Container build instructions
├── docker-compose.yml       # Docker Compose orchestration
├── .env.example             # Environment variables template
├── .gitignore               # Git ignore rules
└── README.md                # This file
```

## API Reference

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login` | Login with username/password, returns JWT token |

### Input Streams

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/inputs` | List all input streams with status |
| POST | `/api/inputs` | Create new input stream |
| PUT | `/api/inputs/{id}` | Update input stream |
| DELETE | `/api/inputs/{id}` | Delete input stream and all outputs |
| POST | `/api/inputs/{id}/start` | Start FFmpeg input process |
| POST | `/api/inputs/{id}/stop` | Stop FFmpeg input process |
| GET | `/api/inputs/{id}/thumbnail` | Get latest thumbnail image |

### Output Streams

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/outputs/{input_id}` | List outputs for an input |
| POST | `/api/outputs` | Create new output stream |
| DELETE | `/api/outputs/{id}` | Delete output stream |
| POST | `/api/outputs/{id}/start` | Start FFmpeg output process |
| POST | `/api/outputs/{id}/stop` | Stop FFmpeg output process |

### Real-time Stats

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/stats` | Current statistics for all streams |
| WS | `/api/ws` | WebSocket for live stats updates (2s interval) |

## SRT URL Format

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

### Testing with low latency

`ffplay` buffers network streams aggressively by default, which can add several seconds of perceived delay. For low-latency testing, launch the consumer with:

```bash
ffplay -fflags nobuffer -flags low_delay "srt://127.0.0.1:6002?mode=caller&latency=200"
```

## Development

### Local Setup (without Docker)

```bash
# 1. Install FFmpeg (system dependency)
# macOS: brew install ffmpeg
# Ubuntu: apt install ffmpeg

# 2. Create virtual environment
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
python main.py
```

### VS Code + Kimi Code Workflow

This project is optimized for development with **VS Code** and **Kimi Code** extension:

1. Open project folder in VS Code
2. Install recommended extensions (see `.vscode/extensions.json`)
3. Use Kimi Code for AI-assisted development
4. Run/Debug via VS Code launch configurations

## Technology Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11, FastAPI, SQLAlchemy |
| Frontend | Vanilla JS, CSS3, WebSocket |
| Database | SQLite (file-based) |
| Streaming | FFmpeg with SRT protocol |
| Container | Docker, Docker Compose |
| Auth | JWT (python-jose), bcrypt |

## Roadmap

- [ ] SRT statistics (RTT, packet loss, bandwidth) via `srt-live-transmit`
- [ ] Multi-user support with role-based access
- [ ] Stream recording to file
- [ ] REST API authentication with API keys
- [ ] Prometheus metrics export
- [ ] Dark/Light theme toggle
- [ ] Mobile-responsive dashboard
- [ ] Go rewrite with native libsrt bindings for production

## License

MIT License — see LICENSE file for details.

## Support

For issues and feature requests, please use the [GitHub Issues](https://github.com/svaganov/srt-restreamer/issues) page.
