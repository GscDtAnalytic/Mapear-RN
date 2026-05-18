"""Cloud Run Job entrypoint for social post embedding — Eixo 2 v2a social.

Glue layer: BQ extract (silver_social_posts) → run_social_embedding CLI →
BQ load (silver_social_post_embeddings). The CLI stays JSONL-pure so it
remains testable without GCP credentials.

WRITE_APPEND semantics: the downstream graph-communities query uses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY content_hash ORDER BY run_at DESC) = 1
to pick the latest embedding per hash, so re-runs with overlapping lookback
windows are safe.

Expected env vars (set by terraform via cloud_run module):
  GCP_PROJECT_ID                          BigQuery project ID
  GCP_BQ_DATASET_SILVER                   silver dataset name
  GCP_GCS_BUCKET_NAME                     GCS bucket for embedding cache
  MAPEAR_REGION                           region slug (default "rn")
  MAPEAR_EMBED_SOCIAL_LOOKBACK_DAYS       lookback window in days (default 2)
  MAPEAR_EMBEDDINGS_SOCIAL_POST_CACHE_GCS_PREFIX  GCS prefix (default "social_post_embeddings/")
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from google.cloud import bigquery


SCHEMAS_DIR = Path(os.environ.get("EMBED_SOCIAL_SCHEMAS_DIR", "/app/schemas"))

_SILVER_SOCIAL_QUERY = """
    SELECT
        content_hash,
        text,
        published_at,
        @region    AS region,
        NULL       AS tenant_id
    FROM `{project}.{silver_ds}.silver_social_posts`
    WHERE published_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback_days DAY)
      AND text IS NOT NULL
      AND text != ''
"""
# Note: silver_social_posts has neither region nor tenant_id as BQ columns
# (Eixo 4 v2 territory). We stamp them as job-level constants in the SELECT
# and do NOT filter by region in WHERE — the --region flag on the CLI does
# the Python-level filter instead. Same pattern as graph_runner._resolve_personas.


def _log(msg: str) -> None:
    sys.stderr.write(f"[embed-social] {msg}\n")
    sys.stderr.flush()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _load_schema(table: str) -> list[bigquery.SchemaField]:
    path = SCHEMAS_DIR / f"{table}.json"
    fields = json.loads(path.read_text(encoding="utf-8"))
    return [
        bigquery.SchemaField(f["name"], f["type"], mode=f.get("mode", "NULLABLE"))
        for f in fields
    ]


def _extract_social_to_jsonl(
    client: bigquery.Client,
    project: str,
    silver_ds: str,
    region: str,
    lookback_days: int,
    out_path: Path,
) -> int:
    query = _SILVER_SOCIAL_QUERY.format(project=project, silver_ds=silver_ds)
    params = [
        bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        bigquery.ScalarQueryParameter("region", "STRING", region),
    ]
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    rows = client.query(query, job_config=job_config).result()
    count = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            payload = {k: _json_safe(v) for k, v in dict(row).items()}
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            count += 1
    return count


def _gcs_storage_available() -> bool:
    try:
        from google.cloud import storage as _  # noqa: F401

        return True
    except ImportError:
        return False


def _embed_social_posts(
    posts_path: Path,
    out_path: Path,
    region: str,
    gcs_bucket: str,
    cache_prefix: str,
    project_id: str,
) -> None:
    cmd = [
        sys.executable,
        "-m",
        "mapear_nlp.graph.run_social_embedding",
        "--posts",
        str(posts_path),
        "--out",
        str(out_path),
        "--region",
        region,
        "--project-id",
        project_id,
    ]
    use_cache = bool(gcs_bucket) and _gcs_storage_available()
    if use_cache:
        cmd.extend(["--cache-bucket", gcs_bucket, "--cache-prefix", cache_prefix])
    else:
        cmd.append("--no-cache")
        if gcs_bucket and not use_cache:
            _log("google-cloud-storage not installed — running without GCS cache")
    _log(f"invoking: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _load_jsonl_to_bq(
    client: bigquery.Client,
    jsonl_path: Path,
    table_id: str,
    schema_table: str,
) -> int:
    schema = _load_schema(schema_table)
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        ignore_unknown_values=False,
    )
    with jsonl_path.open("rb") as fh:
        job = client.load_table_from_file(fh, table_id, job_config=job_config)
    job.result()
    return job.output_rows or 0


def main() -> int:
    project = _required_env("GCP_PROJECT_ID")
    silver_ds = _required_env("GCP_BQ_DATASET_SILVER")
    region = os.environ.get("MAPEAR_REGION", "rn").strip() or "rn"
    lookback_days = int(os.environ.get("MAPEAR_EMBED_SOCIAL_LOOKBACK_DAYS", "2"))
    gcs_bucket = os.environ.get("GCP_GCS_BUCKET_NAME", "")
    cache_prefix = os.environ.get(
        "MAPEAR_EMBEDDINGS_SOCIAL_POST_CACHE_GCS_PREFIX", "social_post_embeddings/"
    )

    job_run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()
    _log(
        f"start project={project} region={region} lookback={lookback_days}d "
        f"bucket={gcs_bucket or '(no-cache)'} run_id={job_run_id} at={started_at}"
    )

    client = bigquery.Client(project=project)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        posts_path = tmp_dir / "posts.jsonl"
        embeddings_path = tmp_dir / "embeddings.jsonl"

        n_posts = _extract_social_to_jsonl(
            client, project, silver_ds, region, lookback_days, posts_path
        )
        _log(f"extracted {n_posts} posts from silver_social_posts")
        if n_posts == 0:
            _log("no posts — exiting 0")
            return 0

        _embed_social_posts(
            posts_path, embeddings_path, region, gcs_bucket, cache_prefix, project
        )

        if not embeddings_path.exists() or embeddings_path.stat().st_size == 0:
            _log("no embeddings emitted — exiting 0")
            return 0

        table_id = f"{project}.{silver_ds}.silver_social_post_embeddings"
        n_loaded = _load_jsonl_to_bq(
            client, embeddings_path, table_id, "silver_social_post_embeddings"
        )
        _log(f"loaded {n_loaded} embedding rows into {table_id}")

    _log(f"done run_id={job_run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
