# Mapear-RN

**English** · [Português](README.pt.md)

> Production ETL platform for socio-political monitoring of Rio Grande do Norte, Brazil. 167 municipalities, multi-source, running in production for under R$ 5/month (about US$1).

[![CI](https://github.com/GscDtAnalytic/Mapear-RN/actions/workflows/ci.yml/badge.svg)](https://github.com/GscDtAnalytic/Mapear-RN/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![uv workspace](https://img.shields.io/badge/uv-workspace-purple)]()
[![dbt 1.11](https://img.shields.io/badge/dbt-1.11.8-orange)]()
[![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC)]()
[![GCP](https://img.shields.io/badge/cloud-GCP-4285F4)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green)]()

## What it is, in one line

A production data pipeline that collects what regional news portals and social networks publish about the mayors, council members, and political narratives of all 167 municipalities in Rio Grande do Norte, enriches it with Portuguese NLP, models it in a data warehouse, and serves it through an API and a dashboard.

This README is written for tech leads and recruiters who want to understand, in three to five minutes, what was built, with which technologies, and why. The focus is on cost-benefit trade-offs and the decisions that actually mattered. Deep technical detail for each component lives in the subproject READMEs linked at the end.

## Table of contents

- [TL;DR](#tldr)
- [Architecture in 30 seconds](#architecture-in-30-seconds)
- [Engineering philosophy](#engineering-philosophy)
- [Tech stack](#tech-stack)
- [Cost-benefit decisions](#cost-benefit-decisions)
- [The incident that shaped the project](#the-incident-that-shaped-the-project)
- [Data layers (medallion)](#data-layers-medallion)
- [Running it locally](#running-it-locally)
- [Repository structure](#repository-structure)
- [Roadmap and status](#roadmap-and-status)
- [Milestones](#milestones)
- [License](#license)

## TL;DR

| Question | Short answer |
|---|---|
| What is the problem? | Socio-political coverage of the 167 municipalities of RN is scattered across regional portals and social networks. There is no time series and no aggregation. |
| What is the solution? | Collect (RSS + Apify), standardize, enrich with deterministic Portuguese NLP, and model with dbt in a cloud warehouse. |
| Which sources? | News portals (RSS) plus Facebook, Instagram, X, and TikTok (Apify). YouTube is planned. |
| Where does it run? | Google Cloud Platform. Cloud Run Jobs triggered by Cloud Scheduler, with data in GCS and BigQuery. |
| What does it cost? | Under R$ 5/month in production. The same architecture on an always-on Composer/Airflow setup would pass R$ 150/month. |
| Who consumes it? | A React dashboard and a FastAPI service over BigQuery, with dbt models ready for BI. |

## Architecture in 30 seconds

```
                ┌───────────────────────────────────────────────────────────────┐
                │  SOURCES                                                      │
                │  RSS feeds  •  Facebook / Instagram / X / TikTok (via Apify)  │
                └────────────────────────────┬──────────────────────────────────┘
                                             │
                                             ▼
              ┌──────────────────────────────────────────────────────────┐
              │  INGESTION — Cloud Run Jobs (cron via Cloud Scheduler)   │
              │  • mapear-rss      — every 8h                           │
              │  • mapear-social   — cadence varies per network         │
              └────────────────────────────┬─────────────────────────────┘
                                           │
                  ┌────────────────────────┴────────────────────────┐
                  ▼                                                 ▼
        ┌──────────────────┐                            ┌──────────────────────┐
        │  GCS (data lake) │ ──── reprocessing ───────► │  BigQuery (warehouse) │
        │  raw • silver    │                            │  silver • gold • marts │
        │  Parquet         │                            │  partitioned +         │
        │                  │                            │  clustered             │
        └──────────────────┘                            └──────────┬───────────┘
                                                                   │
                  ┌────────────────────────────────────────────────┘
                  ▼                                                ▼
        ┌──────────────────────────┐                  ┌──────────────────────────┐
        │  dbt (mapear_rn)         │                  │  Cloud Run Services      │
        │  staging → intermediate  │                  │  • freshness-emitter     │
        │  → marts (fct_* / dim_*) │                  │  • alert-runner          │
        └──────────┬───────────────┘                  │  • nlp-runner / graph    │
                   │                                  └──────────────────────────┘
                   ▼
        ┌──────────────────────────────────────┐
        │  Consumption layer                   │
        │  • FastAPI  ────────►  REST /API     │
        │  • React+Vite SPA   ►  dashboard.web │
        │  • Looker BI (optional)              │
        └──────────────────────────────────────┘
```

## Engineering philosophy

Five principles govern the codebase. They also explain most of the cost and reliability decisions further down.

1. **Stateless workers.** Each pipeline is a container that starts, processes, writes, and exits. There are no idle servers on the bill.
2. **Lake before warehouse.** Everything is persisted to GCS as Parquet before it reaches BigQuery, so reprocessing costs nothing.
3. **Schema as code.** BigQuery tables are versioned Terraform resources, and a pre-commit hook blocks drift between the declared schema and the code.
4. **No LLMs in production.** NLP is deterministic (regex, gazetteers, dictionaries), which keeps it predictable, auditable, and free of per-inference cost.
5. **Fail loud.** A load that writes zero rows when rows are expected is an error, not a success. This rule was paid for in production (see [the incident](#the-incident-that-shaped-the-project)).

## Tech stack

Each choice is justified in a line. Where there is an obvious alternative, it is named.

### Language and tooling

| Technology | Version | Why |
|---|---|---|
| Python | 3.11–3.12 | Mature data ecosystem, type hints, native async. |
| uv (workspace) | 0.4+ | Resolves in seconds, single lock file for the whole monorepo. Replaced Poetry after Phase 6: installs roughly 10x faster and removes lock-file divergence between subprojects. |
| import-linter | 2.0+ | Layered architecture contracts checked in CI (`domain ◄ infra ◄ {storage,nlp,mlops} ◄ pipelines`). Without it the structure degrades within weeks. |

### Collection and parsing

| Technology | Version | Why |
|---|---|---|
| feedparser | 6.0+ | The de facto standard for RSS, with edge cases that would take weeks to reimplement. |
| trafilatura | 1.8.1 | Extracts clean article content from news portals, better than readability-lxml on Brazilian Portuguese. |
| httpx | 0.27 | Async HTTP with timeouts, retries, and HTTP/2. A direct replacement for requests under concurrency. |
| playwright | 1.45 (optional) | Headless browser for portals with anti-bot measures. Loaded on demand, not in every job. |
| Apify | API v2 | Managed scrapers for Facebook, Instagram, X, and TikTok. Building our own would violate the terms of service and cost more than Apify's R$ 50-100/month. |

### NLP, fully deterministic

| Technology | Version | Why |
|---|---|---|
| spaCy | 3.7+ | Portuguese NER and POS tagging with pretrained models. Zero inference cost on CPU. |
| sentence-transformers | 3.1+ | Embeddings for narrative clustering (Axis 2). Runs on CPU at our volume. |
| HDBSCAN | 0.8+ | Hierarchical clustering with no predefined `k`, which is essential for emergent topics. |
| BERTopic | 0.16+ | Topic modeling combining embeddings, HDBSCAN, and c-TF-IDF. |
| PyYAML | 6.0+ | NER post-processing rules and gazetteers kept in versioned YAML, auditable by a human. |
| anthropic | 0.39+ | Claude API used only to explain clusters to the end user (Axis 2), never as a production inference step. |

Production NLP is deterministic on purpose: cost is zero per document, every classification can be justified by reading a rule, the same input always yields the same output, and latency stays under 100 ms per document on CPU. An LLM only enters as an explanation layer for the human reading the dashboard.

### Data warehouse and lake

| Technology | Version | Why |
|---|---|---|
| BigQuery | on-demand | Pay per query, partitioned by date and clustered by municipality, so dashboards cost under R$ 0.01 per query. Snowflake or Redshift would need an always-on warehouse near R$ 200+/month. |
| Google Cloud Storage | — | Parquet data lake (`raw/silver/gold`). Standard storage costs cents per GB. |
| DuckDB | via dbt-duckdb 1.10.1 | The same SQL runs locally in dev and in the cloud in prod. Zero cost during development. |
| Apache Iceberg | 0.8+ (optional) | Time travel and schema evolution for critical datasets. Loaded only where needed. |
| PyArrow | 17.0+ | Parquet serialization and columnar in-memory format. |

### Transformation

| Technology | Version | Why |
|---|---|---|
| dbt-core | 1.11.8 | The market standard for SQL transformation: tests, lineage, generated docs, macros. |
| dbt-bigquery | 1.11.1 | Adapter for the production target. |
| dbt-duckdb | 1.10.1 | Adapter for local dev: same SQL model, different target. |
| sqlfluff | 3.1+ | SQL linter. A pre-commit hook blocks dialect-specific syntax (for example, `INTERVAL 'N unit'` is DuckDB-only). |

### Cloud (GCP)

| Technology | Why |
|---|---|
| Cloud Run Jobs | The container runs, processes, and exits. Pay per execution, about R$ 0.50/month for a 2-minute run every 8 hours. |
| Cloud Scheduler | Cron as a service. Free for up to 3 jobs/month, which covers everything here. |
| Cloud SQL (PostgreSQL 15) | Operational metadata (auth logs, alerts). A `db-f1-micro` instance at about R$ 35/month is the largest line on the bill. |
| Memorystore (Redis) | Dedup cache and circuit-breaker state. Replaceable by Redis on a VM if cost becomes a concern. |
| Secret Manager | Versioned, IAM-controlled secrets. The free tier covers usage. |
| Workload Identity Federation | GitHub Actions authenticates to GCP without static keys. No long-lived credentials. |
| Artifact Registry | Container images per pipeline. The free tier covers usage. |

### Backend and frontend

| Technology | Version | Why |
|---|---|---|
| FastAPI | 0.110+ | Native async, free OpenAPI, Pydantic validation. Runs on Cloud Run with a cold start under 2s. |
| uvicorn | 0.27+ | Standard ASGI server. |
| React | 18.3 | Static SPA hostable on GCS plus Cloud CDN for about R$ 0.50/month. |
| Vite | 5.2+ | Sub-second builds, instant HMR. The obvious replacement for Create React App. |
| Recharts | 2.12+ | Declarative charts, enough for this case. Raw D3 was not worth the added complexity. |
| Leaflet + react-leaflet | 1.9.4 / 4.2 | Open-source maps with no API key and no quota. |
| TanStack Query | 5.40+ | Server-state cache on the client, cutting BigQuery calls by about 70%. |
| TailwindCSS | 3.4+ | Utility-first. Final bundle under 30 KB gzipped. |
| TypeScript | 5.4+ | Type safety on the front, matching what Pydantic provides on the back. |

### Observability and resilience

| Technology | Why |
|---|---|
| loguru | Structured JSON logging without boilerplate. A direct replacement for the stdlib logging module. |
| tenacity | Retry decorator with exponential backoff and jitter. One line of decorator instead of twenty lines of loop. |
| prometheus-client | Prometheus-format metrics, exported through Cloud Monitoring. |
| Cloud Monitoring + Alert Policies | Three critical policies: silver/gold freshness, BQ load failures, and schema drift. |

### IaC and CI/CD

| Technology | Why |
|---|---|
| Terraform | All GCP infrastructure is code, reviewed in pull requests. State is remote in GCS. |
| GitHub Actions | The free tier covers the project. Per-subproject change detection avoids unnecessary rebuilds. |
| pre-commit | Local hooks block dialect-specific SQL, secrets, and ruff/black violations. |
| docker-compose | Local Postgres and Redis with the same configuration as production, no internet required. |

## Cost-benefit decisions

Five choices that set this project apart from a tutorial-default setup.

### 1. Cloud Run Jobs instead of Composer/Airflow

The pipelines run for two to five minutes, a few times a day. Keeping an always-on Airflow (at least 1 vCPU, 2 GB RAM, 24/7) would cost R$ 150-300/month, between 30x and 60x the total cost of the project. Cloud Run Jobs charge only for execution, around R$ 0.50/month.

The accepted trade-off is no DAGs with complex inter-task dependencies. Each pipeline here is a short linear graph, so Cloud Scheduler is enough. If that changes, the `services/` directory is ready to host a Cloud Workflows or Argo-style orchestrator without rewriting the pipelines.

### 2. DuckDB locally, BigQuery in production, same SQL

dbt runs with two targets: `dev` (DuckDB) and `prod` (BigQuery). During development, `dbt build` runs in milliseconds against a local file, with zero risk of burning billable quota while testing changes. In production, BigQuery does the heavy lifting with partitioning and clustering.

A pre-commit hook blocks dialect-specific syntax (for example, `INTERVAL '1 day'` is DuckDB-only and breaks on BQ), and dbt macros such as `{{ dbt.dateadd(...) }}` abstract date arithmetic. The result is one SQL codebase, two targets, and no silent divergence.

### 3. Deterministic NLP, no LLMs in production

NER, sentiment, and topic classification run through spaCy, YAML dictionaries, and Python rules. Inference cost is zero per document, any classification can be justified by reading the rule, the same input always produces the same output, and latency stays under 100 ms per document on CPU.

There is room for an LLM, but as an explainer. When a narrative cluster appears on the dashboard, the user can ask the system to explain it, and only then does the Claude API run. That is one call per human interaction, not one per document.

### 4. GCS data lake before BigQuery

All raw data lands in GCS as Parquet before it is loaded into BigQuery. Reprocessing then costs nothing: if enrichment logic changes, the dbt models are rewritten against the same Parquet files. Without this layer, every production bug would force re-ingestion from sources, which is often impossible since RSS feeds keep no history. The lake also preserves raw data by default, so a question like "what did this article look like before cleaning?" always has an answer.

### 5. Workload Identity Federation, no static keys

GitHub Actions authenticates to GCP over OIDC, with no service-account keys in secrets. This removes an entire class of leak, the "key leaked in a log or repo" failure. Each deploy is signed by the workflow identity and branch, and there is no key to rotate because there is no key. The trade-off is a more complex initial setup than a JSON file in a GitHub secret, but it is configured once and left alone.

## The incident that shaped the project

On 18 April 2026, for about 24 hours, the RSS pipeline ran normally on Cloud Run (`succeededCount=1` on every execution), but no new rows reached BigQuery. The dashboards went stale. No alert fired.

The root cause was a missing flag in the Parquet load configuration (`ParquetOptions.enable_list_inference`), which made the BigQuery load job reject every file for schema mismatch, with no error, just zero rows loaded. The pipeline reported success because extraction worked and the load "completed" with zero rows.

What changed afterward:

1. **Freshness emitter.** A dedicated Cloud Run Job runs every 30 minutes, reads `__TABLES__.last_modified_time`, and publishes `custom.googleapis.com/mapear/freshness_minutes`. An alert fires if silver or gold goes past N minutes without an update.
2. **Schemas as code.** BigQuery tables are now Terraform resources with versioned JSON schema. Pre-commit checks that code changes match the declared schema.
3. **Fail loud by default.** Pipelines propagate exceptions, and a load with zero rows in a window where N rows are expected is treated as an error.
4. **Parameterized dbt drift tests.** Each critical model has a test comparing the current schema to the expected one.

The lesson recorded in the project: fail-loud is a quality requirement, not a convenience. Metrics that silence errors are worse than no metrics. Full post-mortem in [`docs/diagnostico/2026-04-18/`](docs/diagnostico/2026-04-18/).

## Data layers (medallion)

```
┌─────────┐   ┌──────────┐   ┌────────┐   ┌──────────────────┐
│  raw    │ → │  silver  │ → │  gold  │ → │  marts (fct/dim) │
│  (GCS)  │   │  (stg_*) │   │ (int_*)│   │  (fct_*/dim_*)   │
└─────────┘   └──────────┘   └────────┘   └──────────────────┘
 immutable     clean,         cross-source  dimensional,
 bytes         deduplicated   join          BI-ready
```

Why this separation matters for cost:

- **raw** is write-once. We never recollect to reprocess, we only re-run transformations over the existing Parquet.
- **silver** runs only over the new delta (via watermark), so history is not reprocessed on every execution.
- **gold** joins RSS and Social via `source_type`, resolves identities (`"mayor of Mossoró"` to a canonical ID), and materializes the heavy models.
- **marts** are the tables the dashboard queries. Partition by date plus cluster by municipality keeps a typical scan under 1 MB.

Naming conventions: `stg_<source>__<entity>` (staging), `int_<domain>__<description>` (intermediate), `fct_<fact>` / `dim_<dimension>` (marts). Details in [`dbt/README.md`](dbt/README.md).

## Running it locally

Prerequisites: Python 3.11+, Docker and docker-compose, `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`), and `make`.

```bash
# 1. Start Postgres + Redis (same config as production)
make up

# 2. Install the whole workspace (a single .venv for everything)
make install-all

# 3. Run the RSS pipeline locally (dev target = DuckDB)
make rss-pipeline

# 4. Run dbt
make dbt-build

# 5. Start the dashboard (API + frontend)
cd apps/dashboard && make dev
```

For per-subproject detail, see the specific READMEs:

- ETLs: [`pipelines/mapear-rss/README.md`](pipelines/mapear-rss/README.md), [`pipelines/mapear-social/README.md`](pipelines/mapear-social/README.md)
- Dashboard: [`apps/dashboard/README.md`](apps/dashboard/README.md)
- Modeling: [`dbt/README.md`](dbt/README.md)
- Infra: [`infra/README.md`](infra/README.md)

## Repository structure

```
Mapear-RN/
├── libs/                    # Shared libraries (pure Python, testable)
│   ├── mapear-domain/       # RN entities, person resolution, source of truth for the 167 municipalities
│   ├── mapear-infra/        # Config (Pydantic), logging (loguru), retry, cache, circuit breaker
│   ├── mapear-nlp/          # Deterministic NER + sentiment + topics in Brazilian Portuguese
│   ├── mapear-storage/      # BigQuery/GCS/DuckDB loaders, watermark, idempotency
│   └── mapear-mlops/        # MLflow for NLP evaluation and baselines
│
├── pipelines/               # ETL applications (orchestrate libs + credentials)
│   ├── mapear-rss/          # RSS: discovery → extraction (trafilatura) → enrichment → load
│   └── mapear-social/       # Apify (FB/IG/X/TikTok) → enrichment → load
│
├── services/                # Auxiliary Cloud Run Jobs (alert, freshness, dbt-runner, nlp-runner, graph)
│
├── apps/
│   └── dashboard/           # FastAPI (api/) + React/Vite SPA (frontend/)
│
├── dbt/                     # dbt project (mapear_rn): staging → intermediate → marts
│   ├── models/
│   ├── seeds/               # rn_cities_mayors.csv, source of truth for the 167 municipalities
│   └── tests/singular/      # 20+ quality tests
│
├── infra/                   # Terraform (modules per GCP resource)
│
├── docs/                    # Post-mortems, quality baselines, runbooks, tech debt
│
├── .github/workflows/       # CI (lint + test + import-linter) and CD (build + deploy)
│
├── Makefile                 # Centralized targets
├── pyproject.toml           # uv workspace root
└── docker-compose.yml       # Local Postgres + Redis
```

Each subproject has its own README with its specific decisions. Start with the ones linked under [Running it locally](#running-it-locally).

## Roadmap and status

| Axis | Status | Next step |
|---|---|---|
| Axis 1 — Coverage (RSS + Social) | ✅ In production | Add the YouTube Data API. |
| Axis 2 — Narratives (clustering, embeddings, LLM explanation) | 🟡 In progress | Materialize `fct_narrative_cluster_*` and expose it in the dashboard. |
| Axis 3 — Communities (author co-activation graphs) | 🟡 In progress | `graph-runner` is running; integrate the marts into the dashboard. |
| Data quality | ✅ 35 fields / 39 metrics | Close the documented technical debt across 9 gaps. |
| Observability | ✅ Freshness + 3 alerts | Add Cloud Monitoring dashboards for SLOs. |

Technical debt is documented and prioritized in [`docs/tech_debt_INDEX.md`](docs/tech_debt_INDEX.md).

## Milestones

The repository documents the process, not only the final product.

- **Milestone 0.** Scope definition: RN, 167 municipalities, seed CSV as the single source of truth.
- **Milestone 1.** First RSS pipeline in production (Cloud Run Job + Scheduler).
- **Milestone 2.** The April 2026 incident and the observability layer built in response.
- **Milestone 3.** Data quality sprint (35 derived fields, 39 metrics, baselines in production).
- **Milestone 4.** Expansion to social (Apify) and generalization of the warehouse to multi-source.
- **Milestone 5.** Re-architecture to a uv workspace with layered libs and import-linter, in 9 incremental phases ([`ARCHITECTURE_PROPOSAL.md`](ARCHITECTURE_PROPOSAL.md)).
- **Milestone 6.** Consumption layer: FastAPI API + React SPA.

To contribute, read [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT. Free for portfolio, teaching, journalism, and research. Attribution appreciated.

## Contact

- Author: Guilherme Santos Cavalcante
- Email: gui.cavalcante3o@gmail.com
- Repository: [github.com/GscDtAnalytic/Mapear-RN](https://github.com/GscDtAnalytic/Mapear-RN)
