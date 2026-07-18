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

# Create data directories and a non-root user to run the bot as
RUN mkdir -p data media \
    && groupadd -r bot && useradd -r -g bot -d /app bot \
    && chown -R bot:bot /app

USER bot

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sqlite3; sqlite3.connect('/app/data/bot.db').execute('SELECT 1')"

# Run the bot
CMD ["python", "-m", "src.main"]

