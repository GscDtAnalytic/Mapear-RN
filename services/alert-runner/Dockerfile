# Cloud Run Job — spike + CIB alerting.
# Build context: repo root (uv workspace). Installs only the
# mapear-alert-runner subtree (mapear-infra + BigQuery client) — no torch/spacy.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package mapear-alert-runner

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/services/alert-runner
CMD ["python", "run_alerts.py"]
