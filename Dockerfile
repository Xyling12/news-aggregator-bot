FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create data directories
RUN mkdir -p data media

# Run the bot
CMD ["python", "-m", "src.main"]

