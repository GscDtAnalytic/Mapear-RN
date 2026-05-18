# Cloud Run Job — RSS pipeline (discover → extract → silver → dbt → quality).
# Build context: repo root. Installs only the mapear-rss subtree
# (libs/mapear-{domain,infra,nlp,storage} + dependencies declared by
# pipelines/mapear-rss/pyproject.toml).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    ENVIRONMENT=production \
    ENRICHMENT_MODE=api

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package mapear-rss --extra gcp

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app/pipelines/mapear-rss
ENTRYPOINT ["python", "-m", "mapear_rss"]
CMD []
