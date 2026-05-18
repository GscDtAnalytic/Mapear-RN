.PHONY: setup install up down logs \
        fmt lint lint-imports check \
        test test-domain test-infra test-storage test-nlp test-social test-rss test-services test-apps \
        dbt-seed dbt-run dbt-test dbt-build dbt-docs \
        rss-pipeline social-pipeline rss-backfill \
        seed-feeds backfill \
        schemas schemas-check \
        eval eval-update-baseline eval-mlflow mlflow-ui shadow-compare \
        narrative-eval coactivation-eval community-eval detect-communities \
        author-resolution-eval resolve-personas \
        narrative-cluster-eval cluster-narratives \
        stance-eval classify-stances \
        embed-social-posts \
        query-narratives create-vector-index \
        target-list target-validate target-add target-remove \
        run-alerts check-image-drift

# Workspace root — uv resolves the single .venv from here.
# Every uv invocation uses --no-sync (called once by `install`) for speed.
UV_RUN  := uv run --no-sync

# --- Setup ---
install:
	uv sync --all-packages --all-extras

setup: install
	uv run --no-sync pre-commit install
	cp -n .env.example .env 2>/dev/null || true
	@echo "Setup completo. Edite o .env conforme necessário."

# --- Docker Compose ---
up:
	docker compose up -d
	@echo "Airflow UI: http://localhost:8080 (admin/admin)"

down:
	docker compose down

logs:
	docker compose logs -f

# --- Formatação e Lint ---
LINT_PATHS := libs pipelines services apps tools dags

fmt:
	$(UV_RUN) black $(LINT_PATHS)
	$(UV_RUN) ruff check --fix $(LINT_PATHS)

lint:
	$(UV_RUN) black --check $(LINT_PATHS)
	$(UV_RUN) ruff check $(LINT_PATHS)
	$(UV_RUN) sqlfluff lint dbt/models/ || true

# Fase 6 — import-linter contracts (layered architecture).
lint-imports:
	$(UV_RUN) lint-imports

check: lint lint-imports test dbt-test

# --- Testes ---
# `test` runs every pacote do workspace; sub-targets isolam por área.
test: test-domain test-infra test-storage test-nlp test-mlops test-social test-rss test-services

test-domain:
	$(UV_RUN) --package mapear-domain pytest libs/mapear-domain/tests/ -v

test-infra:
	$(UV_RUN) --package mapear-infra pytest libs/mapear-infra/tests/ -v

test-storage:
	$(UV_RUN) --package mapear-storage pytest libs/mapear-storage/tests/ -v

test-nlp:
	$(UV_RUN) --package mapear-nlp pytest libs/mapear-nlp/tests/ -v

test-mlops:
	$(UV_RUN) --package mapear-mlops pytest libs/mapear-mlops/tests/ -v

test-social:
	$(UV_RUN) --package mapear-social pytest pipelines/mapear-social/tests/ -v

test-rss:
	$(UV_RUN) --package mapear-rss pytest pipelines/mapear-rss/tests/ -v

test-services:
	@for d in services/*/; do \
	  if ls $$d*.py >/dev/null 2>&1; then \
	    name=$$(basename $$d); \
	    echo "--- $$name ---"; \
	    (cd $$d && $(UV_RUN) pytest -v --tb=short) || exit $$?; \
	  fi \
	done

# --- dbt ---
dbt-seed:
	cd dbt && $(UV_RUN) dbt seed --target dev

dbt-run:
	cd dbt && $(UV_RUN) dbt run --target dev

dbt-test:
	cd dbt && $(UV_RUN) dbt test --target dev

dbt-build:
	cd dbt && $(UV_RUN) dbt build --target dev

dbt-docs:
	cd dbt && $(UV_RUN) dbt docs generate && $(UV_RUN) dbt docs serve

# --- Pipelines (local runs) ---
rss-pipeline:
	ENVIRONMENT=local $(UV_RUN) --package mapear-rss python -m mapear_rss

PLATFORM ?= facebook
social-pipeline:
	ENVIRONMENT=local SOCIAL_PLATFORM=$(PLATFORM) \
	  $(UV_RUN) --package mapear-social python -m mapear_social --platform=$(PLATFORM)

pipeline-local: rss-pipeline

# --- Seeds / Backfill ---
seed-feeds:
	$(UV_RUN) --package mapear-rss python pipelines/mapear-rss/scripts/seed_feeds.py

backfill:
	$(UV_RUN) --package mapear-rss python pipelines/mapear-rss/scripts/backfill.py

GCP_PROJECT           ?= your-gcp-project
GCP_REGION            ?= southamerica-east1
BACKFILL_DATE         ?= 2026-01-01
BATCH_SIZE            ?= 5
BACKFILL_TASK_TIMEOUT ?= 3600
rss-backfill:
	gcloud run jobs execute mapear-rss-pipeline \
	  --project=$(GCP_PROJECT) \
	  --region=$(GCP_REGION) \
	  --args="python,-m,mapear_rss,--backfill-start-date=$(BACKFILL_DATE),--batch-size=$(BATCH_SIZE)" \
	  --task-timeout=$(BACKFILL_TASK_TIMEOUT) \
	  --async
	@echo "Job iniciado em modo backfill desde $(BACKFILL_DATE) (timeout=$(BACKFILL_TASK_TIMEOUT)s)."
	@echo "Acompanhe em: https://console.cloud.google.com/run/jobs?project=$(GCP_PROJECT)"

# --- Eval (CI gates) ---
# Runs libs/mapear-nlp/eval/run.py against the gold-set and gates on F1
# macro vs baseline. After a deliberate rule change, run
# `make eval-update-baseline` and commit the new baseline alongside
# the threshold change. See docs/decisions/adr-eval-harness-political-sentiment.md.
eval:
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.run

eval-update-baseline:
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.run --update-baseline

eval-mlflow:
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.run --mlflow

# --- Narrative explainer eval (Eixo 2 v1) — real LLM calls, NOT in CI.
narrative-eval:
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.narrative_run

coactivation-eval:
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.coactivation_run

community-eval:
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.community_run

# Out-of-band community-detection — ACTIVATIONS=... required.
detect-communities:
	$(UV_RUN) --package mapear-nlp python -m mapear_nlp.graph.run_community_detection \
		--activations $(ACTIVATIONS) \
		$(if $(OUT),--out $(OUT),) \
		$(if $(REGION),--region $(REGION),) \
		$(if $(PERSONAS),--personas $(PERSONAS),)

author-resolution-eval:
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.author_resolution_run

resolve-personas:
	$(UV_RUN) --package mapear-nlp python -m mapear_nlp.graph.run_author_resolution \
		--authors $(AUTHORS) \
		$(if $(OUT),--out $(OUT),) \
		$(if $(REGION),--region $(REGION),)

narrative-cluster-eval:
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.cluster_run

cluster-narratives:
	$(UV_RUN) --package mapear-nlp python -m mapear_nlp.clustering.run_narrative_clustering \
		--gold $(GOLD) \
		--out-embeddings $(OUT_EMBEDDINGS) \
		--out-clusters $(OUT_CLUSTERS) \
		$(if $(ALGORITHM),--algorithm $(ALGORITHM),) \
		$(if $(REGION),--region $(REGION),) \
		$(if $(NO_CACHE),--no-cache,)

# Stance eval — real LLM calls, NOT in CI. Requires MAPEAR_LLM_API_KEY.
stance-eval:
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.stance_run

QUERY ?=
K ?= 5
query-narratives:
	@if [ -z "$(QUERY)" ]; then echo "ERROR: set QUERY='...'"; exit 2; fi
	$(UV_RUN) --package mapear-nlp python -m mapear_nlp.rag.run_rag \
		--query "$(QUERY)" \
		--k $(K) \
		$(if $(REGION),--region $(REGION),)

create-vector-index:
	@if [ -z "$(GCP_PROJECT_ID)" ]; then echo "ERROR: set GCP_PROJECT_ID"; exit 2; fi
	bq query --project_id=$(GCP_PROJECT_ID) --use_legacy_sql=false \
		'CREATE VECTOR INDEX IF NOT EXISTS idx_narrative_embeddings \
		ON `$(GCP_PROJECT_ID).mapear_silver.silver_narrative_embeddings`(embedding) \
		OPTIONS (distance_type = '"'"'COSINE'"'"', index_type = '"'"'IVF'"'"')'

classify-stances:
	$(UV_RUN) --package mapear-nlp python -m mapear_nlp.run_stance_classification \
		--gold $(GOLD) \
		--out $(OUT) \
		$(if $(REGION),--region $(REGION),)

POSTS ?=
OUT ?=
embed-social-posts:
	$(UV_RUN) --package mapear-nlp python -m mapear_nlp.graph.run_social_embedding \
		--posts $(POSTS) \
		--out $(OUT) \
		$(if $(REGION),--region $(REGION),)

mlflow-ui:
	$(UV_RUN) --package mapear-mlops mlflow ui --backend-store-uri "file://$(CURDIR)/mlruns"

# --- Stage 2C — Self-serve target management (mapear-domain CLI).
REGION ?=
ROLE ?=
ARGS ?=

target-list:
	@if [ -z "$(REGION)" ]; then echo "ERROR: set REGION="; exit 2; fi
	$(UV_RUN) --package mapear-domain python -m mapear_domain.targets list \
		--region "$(REGION)" $(if $(ROLE),--role $(ROLE),)

target-validate:
	@if [ -z "$(REGION)" ]; then echo "ERROR: set REGION="; exit 2; fi
	$(UV_RUN) --package mapear-domain python -m mapear_domain.targets validate \
		--region "$(REGION)"

target-add:
	@if [ -z "$(REGION)" ] || [ -z "$(ARGS)" ]; then \
		echo "ERROR: set REGION= and ARGS='--person-id … --name … --role …'"; \
		exit 2; \
	fi
	$(UV_RUN) --package mapear-domain python -m mapear_domain.targets add \
		--region "$(REGION)" $(ARGS)

target-remove:
	@if [ -z "$(REGION)" ] || [ -z "$(ARGS)" ]; then \
		echo "ERROR: set REGION= and ARGS='--person-id …'"; \
		exit 2; \
	fi
	$(UV_RUN) --package mapear-domain python -m mapear_domain.targets remove \
		--region "$(REGION)" $(ARGS)

# Stage 1E — shadow A/B comparator. CANDIDATE=path/to/candidate.yaml required.
CANDIDATE ?=
INPUT ?=
MLFLOW ?=
shadow-compare:
	@if [ -z "$(CANDIDATE)" ]; then \
		echo "ERROR: set CANDIDATE=path/to/candidate.yaml"; \
		exit 2; \
	fi
	cd libs/mapear-nlp && $(UV_RUN) python -m eval.shadow \
		--candidate "$(CURDIR)/$(CANDIDATE)" \
		$(if $(INPUT),--input "$(CURDIR)/$(INPUT)",) \
		$(if $(MLFLOW),--mlflow,)

# --- Data contracts: regenerate BQ JSON schemas from Pydantic ---
# Source of truth: mapear-domain Pydantic + mapear-storage TableContract.
# After editing the Pydantic models, run `make schemas` and commit the
# regenerated JSON in the same PR. CI runs `make schemas-check` to
# guarantee the deployed JSON tracks the model.
schemas:
	$(UV_RUN) --package mapear-storage python tools/generate_bq_schemas.py

schemas-check: schemas
	@if ! git diff --quiet -- infra/modules/bigquery/schemas/; then \
		echo ""; \
		echo "ERROR: BQ schemas drifted from Pydantic source of truth."; \
		echo "  Run 'make schemas' locally and commit the regenerated JSON."; \
		echo "  Diff:"; \
		git --no-pager diff -- infra/modules/bigquery/schemas/ | head -80; \
		exit 1; \
	fi

# --- Image drift check (same as Fase 0 version; targets the post-refactor image names) ---
GAR_REGION  ?= southamerica-east1
GAR_REPO    ?= mapear-rn
IMAGES      ?= rss-pipeline social-pipeline dbt-runner nlp-runner graph-runner alert-runner embed-social-runner freshness-emitter dashboard

check-image-drift:
	@echo "=== Image drift check (HEAD: $$(git rev-parse --short HEAD)) ==="
	@HEAD=$$(git rev-parse HEAD); \
	REGISTRY="$(GAR_REGION)-docker.pkg.dev/$(GCP_PROJECT_ID)/$(GAR_REPO)"; \
	DRIFT=0; \
	for img in $(IMAGES); do \
	  IMG_SHA=$$(gcloud artifacts docker images list "$$REGISTRY/$$img" \
	    --filter="tags:latest" --format="value(tags,version)" 2>/dev/null | head -1); \
	  if echo "$$IMG_SHA" | grep -q "$$HEAD"; then \
	    echo "  ✓ $$img  (CI-built, matches HEAD)"; \
	  else \
	    echo "  ✗ $$img  DRIFT — latest is not from HEAD"; \
	    DRIFT=1; \
	  fi; \
	done; \
	if [ "$$DRIFT" -eq 1 ]; then \
	  echo "One or more images are out of sync. Push a code change to trigger CI rebuild."; \
	  exit 1; \
	fi

# Operational: alert runner against prod BigQuery.
run-alerts:
	GCP_PROJECT_ID=$(GCP_PROJECT_ID) \
	GCP_BQ_DATASET_GOLD=$(GCP_BQ_DATASET_GOLD) \
	$(UV_RUN) --package mapear-alert-runner python services/alert-runner/run_alerts.py
