# Cloud Run Job — social-post embedding orchestrator.
# Build context: repo root. Installs the mapear-embed-social-runner subtree
# (mapear-nlp[embeddings,gcp] + google-cloud-storage for the GCS cache).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package mapear-embed-social-runner

ENV PATH="/app/.venv/bin:$PATH" \
    EMBED_SOCIAL_SCHEMAS_DIR=/app/infra/modules/bigquery/schemas
WORKDIR /app/services/embed-social-runner
CMD ["bash", "run.sh"]
