# Pinned base image digest for reproducible builds.
FROM python:3.11-slim

# Install FFmpeg and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for the application
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --shell /usr/sbin/nologin app

# Set working directory
WORKDIR /app

# Install Python dependencies first (leverage layer cache)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application code
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Create data directory for SQLite and thumbnails, owned by the app user
RUN mkdir -p /app/data/thumbnails /app/data/slates \
    && chown -R app:app /app/data

USER app

# Expose web interface port
EXPOSE 8080

# Volume for persistent data
VOLUME ["/app/data"]

# Healthcheck against the unauthenticated liveness endpoint
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
    CMD curl -f http://localhost:8080/health/live || exit 1

# Start application (single worker — process manager is process-local)
CMD ["python", "backend/main.py"]
