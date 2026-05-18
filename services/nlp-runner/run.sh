#!/usr/bin/env bash
# Entrypoint for the mapear-nlp-runner Cloud Run Job.
#
# Selects the NLP job via $NLP_JOB and delegates to orchestrate.py.
# The Cloud Run Job env is set by terraform; we keep this shell layer
# minimal so all real logic stays under unit tests in Python.
#
# Expected env (set by terraform via cloud_run module):
#   NLP_JOB                  cluster-narratives | classify-stances
#   GCP_PROJECT_ID           BigQuery project id
#   GCP_BQ_DATASET_GOLD      gold dataset name (source: gold_articles)
#   GCP_BQ_DATASET_SILVER    silver dataset name (sink)
#   GCP_GCS_BUCKET_NAME      GCS bucket for embedding/stance cache
#   MAPEAR_REGION             region slug (default "rn")
#   MAPEAR_LLM_API_KEY        Anthropic API key (classify-stances only)

set -euo pipefail

: "${NLP_JOB:?NLP_JOB must be set (cluster-narratives | classify-stances)}"
: "${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set}"
: "${GCP_BQ_DATASET_GOLD:?GCP_BQ_DATASET_GOLD must be set}"
: "${GCP_BQ_DATASET_SILVER:?GCP_BQ_DATASET_SILVER must be set}"

echo "[nlp-runner] job=${NLP_JOB} project=${GCP_PROJECT_ID} gold=${GCP_BQ_DATASET_GOLD} silver=${GCP_BQ_DATASET_SILVER} region=${MAPEAR_REGION:-rn}"
exec python orchestrate.py
