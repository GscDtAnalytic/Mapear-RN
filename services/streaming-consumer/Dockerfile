# Cloud Run Service — Pub/Sub push consumer (Eixo 1 v2): NER + sentiment +
# Iceberg writes inline. Build context: repo root. The heaviest service
# image — installs mapear-nlp[nlp,embeddings,llm,gcp] + mapear-storage[iceberg].
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PORT=8080

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package mapear-streaming-consumer

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/services/streaming-consumer
# 2 workers — NLP models are large; more workers risk OOM. Cloud Run
# horizontal scaling handles throughput.
CMD exec gunicorn --bind "0.0.0.0:${PORT}" --workers 2 --timeout 60 main:app
