#!/usr/bin/env python3
"""Level 2 integration test for freshness emitter — A6 Passo 4.

Creates ephemeral BQ fixtures in mapear_test (1h TTL), runs the emitter via
monkey-patch on TRACKED_TABLES, verifies Cloud Monitoring metrics, and drops
the dataset. Never edits main.py.

Usage:
    GCP_PROJECT_ID=your-gcp-project python3 manual_test_level2.py
"""

import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))


from google.cloud import bigquery, monitoring_v3
from loguru import logger

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "your-gcp-project")
DATASET = "mapear_test"
TS = int(time.time())
FIXTURE_A_TABLE = f"fixture_silver_articles_extracted_at_{TS}"
FIXTURE_B_TABLE = f"fixture_gold_lastmod_{TS}"
FIXTURE_A_FQN = f"{DATASET}.{FIXTURE_A_TABLE}"
FIXTURE_B_FQN = f"{DATASET}.{FIXTURE_B_TABLE}"

print(f"Project  : {PROJECT_ID}")
print(f"Dataset  : {DATASET}")
print(f"Fixture A: {FIXTURE_A_FQN}  (extracted_at, 17h stale)")
print(f"Fixture B: {FIXTURE_B_FQN}  (_LAST_MODIFIED, few minutes old)")
print()


# ---------------------------------------------------------------------------
# Step 1 — create dataset
# ---------------------------------------------------------------------------
def step1_create_dataset(bq: bigquery.Client) -> None:
    print("=== STEP 1: Create dataset (1h table TTL) ===")
    ds_ref = bigquery.Dataset(f"{PROJECT_ID}.{DATASET}")
    ds_ref.default_table_expiration_ms = 3600 * 1000
    ds_ref.description = "Ephemeral fixtures for A6 freshness simulation. Auto-expires."
    ds_ref.location = "southamerica-east1"
    ds = bq.create_dataset(ds_ref, exists_ok=False)
    print(f"  Created: {ds.full_dataset_id}")


# ---------------------------------------------------------------------------
# Step 2 — fixture A: extracted_at, 17h stale → expected staleness ~1020 min
# ---------------------------------------------------------------------------
def step2_create_fixture_a(bq: bigquery.Client) -> None:
    print("\n=== STEP 2: Fixture A (extracted_at, 17h stale) ===")
    schema = [
        bigquery.SchemaField("extracted_at", "TIMESTAMP"),
        bigquery.SchemaField("marker", "STRING"),
    ]
    tbl = bigquery.Table(f"{PROJECT_ID}.{FIXTURE_A_FQN}", schema=schema)
    bq.create_table(tbl)

    sql = (
        f"INSERT INTO `{PROJECT_ID}.{FIXTURE_A_FQN}` (extracted_at, marker) "
        f"VALUES (TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 17 HOUR), 'fixture_a6_level2')"
    )
    bq.query(sql).result()

    rows = list(
        bq.query(f"SELECT extracted_at FROM `{PROJECT_ID}.{FIXTURE_A_FQN}`").result()
    )
    print(f"  Inserted row: extracted_at={rows[0][0]}")


# ---------------------------------------------------------------------------
# Step 3 — fixture B: no column, uses __TABLES__.last_modified_time
# ---------------------------------------------------------------------------
def step3_create_fixture_b(bq: bigquery.Client) -> None:
    print("\n=== STEP 3: Fixture B (_LAST_MODIFIED, just created) ===")
    schema = [bigquery.SchemaField("marker", "STRING")]
    tbl = bigquery.Table(f"{PROJECT_ID}.{FIXTURE_B_FQN}", schema=schema)
    bq.create_table(tbl)

    sql = (
        f"INSERT INTO `{PROJECT_ID}.{FIXTURE_B_FQN}` (marker) "
        f"VALUES ('fixture_a6_level2')"
    )
    bq.query(sql).result()
    print(f"  Inserted row into {FIXTURE_B_FQN}")
    print("  Waiting 30s for __TABLES__.last_modified_time to stabilize...")
    time.sleep(30)


# ---------------------------------------------------------------------------
# Step 4 — run emitter with monkey-patched TRACKED_TABLES
# ---------------------------------------------------------------------------
def step4_run_emitter() -> tuple[int, str]:
    print("\n=== STEP 4: Run emitter (monkey-patch) ===")
    import main as emitter_main

    _LAST_MODIFIED = emitter_main._LAST_MODIFIED
    original_tables = list(emitter_main.TRACKED_TABLES)

    emitter_main.TRACKED_TABLES.append((FIXTURE_A_FQN, "extracted_at"))
    emitter_main.TRACKED_TABLES.append((FIXTURE_B_FQN, _LAST_MODIFIED))
    print(
        f"  TRACKED_TABLES: {len(emitter_main.TRACKED_TABLES)} entries "
        f"({len(original_tables)} real + 2 fixtures)"
    )

    # Capture loguru output for the report
    log_buf = io.StringIO()
    handler_id = logger.add(
        log_buf, level="DEBUG", format="{time:HH:mm:ss} {level} {message}"
    )

    os.environ["GCP_PROJECT_ID"] = PROJECT_ID
    try:
        rc = emitter_main.main()
    finally:
        logger.remove(handler_id)
        emitter_main.TRACKED_TABLES[:] = original_tables

    captured = log_buf.getvalue()
    # Print only lines that mention the fixtures or totals
    print("  --- relevant log lines ---")
    for line in captured.splitlines():
        if DATASET in line or "Done at" in line:
            print(f"  {line}")
    print("  --- end log ---")
    print(f"  Emitter exit code: {rc}")
    return rc, captured


# ---------------------------------------------------------------------------
# Step 5 — verify Cloud Monitoring metrics
# ---------------------------------------------------------------------------
def step5_verify_metrics() -> list[dict]:
    print("\n=== STEP 5: Verify Cloud Monitoring metrics ===")
    print("  Waiting 90s for metric propagation...")
    time.sleep(90)

    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{PROJECT_ID}"

    now = time.time()
    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": int(now)},
            "start_time": {"seconds": int(now) - 600},
        }
    )

    found = []
    try:
        pages = client.list_time_series(
            request={
                "name": project_name,
                "filter": 'metric.type="custom.googleapis.com/mapear/freshness_minutes"',
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )
        for series in pages:
            table_label = series.metric.labels.get("table", "")
            if DATASET not in table_label:
                continue
            for point in series.points:
                entry = {
                    "table": table_label,
                    "staleness_minutes": point.value.double_value,
                    "end_time": point.interval.end_time,
                }
                found.append(entry)
                print(
                    f"  METRIC FOUND: table={table_label} "
                    f"staleness={point.value.double_value:.1f}min "
                    f"at {point.interval.end_time}"
                )
    except Exception as exc:
        print(f"  ERROR listing time series: {exc}")

    if not found:
        print("  WARNING: No fixture metric points found within 10 min window!")
    else:
        print(f"  Fixture series found: {len(found)}")

    return found


# ---------------------------------------------------------------------------
# Step 6 — cleanup (R3 + R4)
# ---------------------------------------------------------------------------
def step6_cleanup(bq: bigquery.Client) -> bool:
    print("\n=== STEP 6: Cleanup ===")
    try:
        bq.delete_dataset(
            f"{PROJECT_ID}.{DATASET}", delete_contents=True, not_found_ok=False
        )
        print(f"  bq.delete_dataset({DATASET}) -> OK")
    except Exception as exc:
        print(f"  ERROR dropping dataset: {exc}")
        return False

    try:
        bq.get_dataset(f"{PROJECT_ID}.{DATASET}")
        print(f"  WARNING: dataset {DATASET} still exists after delete!")
        return False
    except Exception:
        print(f"  CLEANUP CONFIRMED: Not found: Dataset {PROJECT_ID}:{DATASET}")
        return True


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    bq = bigquery.Client(project=PROJECT_ID)
    cleanup_ok = False
    emitter_rc = None
    metric_results = []
    emitter_logs = ""

    try:
        step1_create_dataset(bq)
        step2_create_fixture_a(bq)
        step3_create_fixture_b(bq)
        emitter_rc, emitter_logs = step4_run_emitter()
        metric_results = step5_verify_metrics()
    finally:
        cleanup_ok = step6_cleanup(bq)

    print("\n=== SUMMARY ===")
    print(f"Emitter exit code : {emitter_rc}")
    print(f"Fixture metrics   : {len(metric_results)} points found")
    print(
        f"Cleanup           : {'OK' if cleanup_ok else 'FAILED — manual intervention required'}"
    )

    if not cleanup_ok:
        sys.exit(2)
    if emitter_rc not in (0, None):
        sys.exit(1)
