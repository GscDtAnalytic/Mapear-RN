# Cloud Run Job — dbt build against BigQuery prod.
# Build context: repo root. Installs only dbt-core + dbt-bigquery — this
# image deliberately depends on no mapear-* package (no torch/spacy).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    DBT_PROFILES_DIR=/app/dbt

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package mapear-dbt-runner

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/dbt
CMD ["bash", "/app/services/dbt-runner/run.sh"]
