#!/usr/bin/env bash
# Entrypoint for the mapear-graph-runner Cloud Run Job.
#
# Selects v2a (community detection) vs v2b (author resolution) via
# $GRAPH_JOB and delegates to orchestrate.py. The Cloud Run Job env is
# set by terraform; we keep this shell layer minimal so all real logic
# stays under unit tests in Python.
#
# Expected env (set by terraform via cloud_run module):
#   GRAPH_JOB                 resolve-personas | detect-communities
#   GCP_PROJECT_ID            BigQuery project id
#   GCP_BQ_DATASET_SILVER     silver dataset name
#   MAPEAR_REGION             region slug (default "rn")
#   MAPEAR_TENANT_ID          optional tenant filter
#   MAPEAR_CIB_USE_PERSONAS   detect-communities only — consume v2b personas
#   MAPEAR_GRAPH_LOOKBACK_DAYS  query window (default 30 for personas, 2 for communities)

set -euo pipefail

: "${GRAPH_JOB:?GRAPH_JOB must be set (resolve-personas | detect-communities)}"
: "${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set}"
: "${GCP_BQ_DATASET_SILVER:?GCP_BQ_DATASET_SILVER must be set}"

echo "[graph-runner] job=${GRAPH_JOB} project=${GCP_PROJECT_ID} dataset=${GCP_BQ_DATASET_SILVER} region=${MAPEAR_REGION:-rn}"
exec python orchestrate.py
