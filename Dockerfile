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

# Run the bot
CMD ["python", "-m", "src.main"]

