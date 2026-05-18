# Cloud Run Job — emit warehouse-table staleness to Cloud Monitoring.
# Build context: repo root. Installs only BigQuery + Monitoring clients.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package mapear-freshness-emitter

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/services/freshness-emitter
CMD ["python", "main.py"]
