#!/usr/bin/env bash
# Entrypoint for the mapear-dbt Cloud Run Job.
#
# Runs the full dbt build against BigQuery prod:
#   1. dbt deps             — install packages (dbt_utils etc.) into ./dbt_packages
#   2. dbt seed             — load rn_targets, rn_cities_mayors, etc.
#   3. dbt source freshness — abort if any source exceeds its error_after threshold
#   4. dbt run              — materialize staging → intermediate → marts
#   5. dbt test             — schema + custom data-quality tests
#
# Fail-fast: any step exiting non-zero aborts the job so Cloud Run marks
# the execution as failed and the alerting on failed dbt runs fires.
#
# Expected env:
#   DBT_TARGET=prod          (defaults to prod when unset)
#   GCP_PROJECT_ID=your-gcp-project (picked up by dbt/profiles.yml)
# Auth: Cloud Run service account ADC — dbt profile uses method=oauth.

set -euo pipefail

: "${DBT_TARGET:=prod}"

echo "[dbt-runner] target=${DBT_TARGET} project=${GCP_PROJECT_ID:-unset}"

dbt deps --project-dir . --profiles-dir .
dbt seed  --project-dir . --profiles-dir . --target "${DBT_TARGET}" --full-refresh
# Fail-fast before materializing marts on top of stale source data.
# Exits non-zero only when a source exceeds its error_after threshold;
# warn_after violations are logged but do not abort.
dbt source freshness --project-dir . --profiles-dir . --target "${DBT_TARGET}"
dbt run   --project-dir . --profiles-dir . --target "${DBT_TARGET}"
# Tests are best-effort — a failing quality test should page via alerting
# but must not block tomorrow's refresh, so we capture the exit code and
# exit with the dbt-run exit (already 0 if we got here) while surfacing
# the test status in logs.
if ! dbt test --project-dir . --profiles-dir . --target "${DBT_TARGET}"; then
    echo "[dbt-runner] dbt test FAILED — inspect target/manifest + logs"
    exit 2
fi

echo "[dbt-runner] dbt build complete"
