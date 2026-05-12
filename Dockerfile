FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 sqlite3 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-railway.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY CLAUDE.md CLAUDE.md
COPY .claude/ .claude/

ENV PYTHONPATH=/app/src
ENV JARVIS_HOME=/app/jarvis_data
ENV JARVIS_HOST=0.0.0.0

RUN mkdir -p /app/jarvis_data/data /app/jarvis_data/logs \
    /app/jarvis_data/skills /app/jarvis_data/backups \
    /app/jarvis_data/workspace /app/jarvis_data/vault

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "jarvis.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
