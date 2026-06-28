FROM python:3.11-slim

# Install FFmpeg and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory for SQLite and thumbnails
RUN mkdir -p /app/data/thumbnails

# Expose web interface port
EXPOSE 8080

# Volume for persistent data
VOLUME ["/app/data"]

# Start application
CMD ["python", "backend/main.py"]
