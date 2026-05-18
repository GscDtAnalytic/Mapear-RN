#!/usr/bin/env bash
# Entrypoint for the mapear-embed-social-runner Cloud Run Job.
#
# Expected env (set by terraform via cloud_run module):
#   GCP_PROJECT_ID                              BigQuery project ID
#   GCP_BQ_DATASET_SILVER                       silver dataset name
#   GCP_GCS_BUCKET_NAME                         GCS bucket for embedding cache
#   MAPEAR_REGION                               region slug (default "rn")
#   MAPEAR_EMBED_SOCIAL_LOOKBACK_DAYS           lookback window in days (default 2)
#   MAPEAR_EMBEDDINGS_SOCIAL_POST_CACHE_GCS_PREFIX  GCS prefix (default "social_post_embeddings/")

set -euo pipefail

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set}"
: "${GCP_BQ_DATASET_SILVER:?GCP_BQ_DATASET_SILVER must be set}"

echo "[embed-social] project=${GCP_PROJECT_ID} silver=${GCP_BQ_DATASET_SILVER} region=${MAPEAR_REGION:-rn} lookback=${MAPEAR_EMBED_SOCIAL_LOOKBACK_DAYS:-2}d"
exec python orchestrate.py
