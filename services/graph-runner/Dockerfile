# Cloud Run Job — community detection / author resolution over the
# co-activation graph. Build context: repo root. Installs the
# mapear-graph-runner subtree (mapear-nlp[graph,gcp] — networkx + BigQuery;
# no torch/spacy/storage).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package mapear-graph-runner

ENV PATH="/app/.venv/bin:$PATH" \
    GRAPH_RUNNER_SCHEMAS_DIR=/app/infra/modules/bigquery/schemas
WORKDIR /app/services/graph-runner
CMD ["bash", "run.sh"]
