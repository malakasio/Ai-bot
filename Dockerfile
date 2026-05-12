FROM python:3.11-slim

WORKDIR /app

COPY requirements-railway.txt .
RUN pip install --no-cache-dir -r requirements-railway.txt

COPY . .

ENV PYTHONPATH=src
ENV JARVIS_HOME=/tmp/jarvis

CMD ["sh", "-c", "uvicorn jarvis.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
