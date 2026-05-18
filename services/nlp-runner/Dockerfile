# Cloud Run Job — narrative clustering / stance classification.
# Build context: repo root. Installs the mapear-nlp-runner subtree
# (mapear-nlp[embeddings,llm,gcp]). Storage is intentionally excluded.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package mapear-nlp-runner

ENV PATH="/app/.venv/bin:$PATH" \
    NLP_RUNNER_SCHEMAS_DIR=/app/infra/modules/bigquery/schemas
WORKDIR /app/services/nlp-runner
CMD ["bash", "run.sh"]
