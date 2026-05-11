FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-railway.txt .
RUN pip install --no-cache-dir -r requirements-railway.txt

COPY . .

ENV PYTHONPATH=src
ENV JARVIS_HOME=/tmp/jarvis

CMD ["sh", "-c", "uvicorn jarvis.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
