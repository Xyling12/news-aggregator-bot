FROM python:3.11-slim

WORKDIR /app

# ffmpeg is needed to merge/transcode YouTube shorts before re-uploading to VK Clips
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create data directories
RUN mkdir -p data media

# NOTE: intentionally runs as root. The production docker-compose mounts
# pre-existing root-owned named volumes (bot_data/bot_media); switching to a
# non-root USER here would make the bot unable to write its DB/media/logs to
# those already-populated volumes and crash-loop on deploy. If you ever want a
# non-root user, chown the existing volumes to its UID first (one-off on the VPS).

# Read-only healthcheck — never creates a phantom bot.db before the app starts
# (uri=True + mode=ro fails cleanly while the DB is missing, which start_period covers).
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import sqlite3; sqlite3.connect('file:/app/data/bot.db?mode=ro', uri=True).execute('SELECT 1 FROM sources LIMIT 1')"

# Run the bot
CMD ["python", "-m", "src.main"]

