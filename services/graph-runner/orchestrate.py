"""Cloud Run Job entrypoint for the Eixo 3 graph jobs (v2a + v2b).

Two modes, selected by the ``GRAPH_JOB`` env var:

* ``resolve-personas`` — Eixo 3 v2b. BQ ``silver_social_posts`` is
  collapsed into one author record per ``(platform, author_handle)``
  with merged content_hashes; the result is fed to
  ``mapear_nlp.graph.run_author_resolution`` and the persona-member
  rows are appended to ``silver_author_personas``.

* ``detect-communities`` — Eixo 3 v2a. ``silver_author_activations``
  is fed to ``mapear_nlp.graph.run_community_detection``; when
  ``MAPEAR_CIB_USE_PERSONAS=true`` is set the persona lookup is also
  extracted from ``silver_author_personas`` and passed via
  ``--personas``. Rows are appended to ``silver_author_communities``.

This script is the *glue* layer: BQ extract → existing CLI → BQ load.
The CLIs themselves stay JSONL-pure so they remain trivially testable
without GCP credentials. Append semantics + downstream dbt
incremental marts handle dedup; we do not MERGE here on purpose
(the same reasoning that motivated the v1 activations write path).
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


SCHEMAS_DIR = Path(os.environ.get("GRAPH_RUNNER_SCHEMAS_DIR", "/app/schemas"))


def _log(msg: str) -> None:
    sys.stderr.write(f"[graph-runner] {msg}\n")
    sys.stderr.flush()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_schema(table: str) -> list[bigquery.SchemaField]:
    path = SCHEMAS_DIR / f"{table}.json"
    fields = json.loads(path.read_text(encoding="utf-8"))
    return [
        bigquery.SchemaField(f["name"], f["type"], mode=f.get("mode", "NULLABLE"))
        for f in fields
    ]


def _extract_query_to_jsonl(
    client: bigquery.Client,
    query: str,
    params: list[bigquery.ScalarQueryParameter],
    out_path: Path,
) -> int:
    """Run a query and write each row as a JSON line. Returns row count."""
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    rows = client.query(query, job_config=job_config).result()
    count = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            payload = {k: _json_safe(v) for k, v in dict(row).items()}
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            count += 1
    return count


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _load_jsonl_to_bq(
    client: bigquery.Client, jsonl_path: Path, table_id: str, schema_table: str
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


def _resolve_personas(
    client: bigquery.Client,
    project: str,
    silver_ds: str,
    region: str,
    tenant_id: str | None,
) -> int:
    lookback_days = int(os.environ.get("MAPEAR_GRAPH_LOOKBACK_DAYS", "30"))
    posts_table = f"`{project}.{silver_ds}.silver_social_posts`"
    # silver_social_posts has neither tenant_id nor region columns today
    # (Eixo 4 v2 territory). Stamp region/tenant_id as job-level constants
    # in the persona-member output rather than filter on them in BQ.
    #
    # author_in_scope is currently NULL for all rows even though scope_status
    # is populated correctly — the writer is not persisting the computed
    # field. Filter on scope_status directly so the graph runner is decoupled
    # from that upstream bug.
    query = f"""
        SELECT
          platform,
          author_handle AS author_id,
          ANY_VALUE(author_display_name) AS display_name,
          ANY_VALUE(author_verified) AS verified,
          ANY_VALUE(author_base_city) AS base_city,
          ARRAY_AGG(DISTINCT content_hash IGNORE NULLS) AS content_hashes,
          @region AS region,
          @tenant_id AS tenant_id
        FROM {posts_table}
        WHERE DATE(published_at)
              BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
              AND CURRENT_DATE()
          AND scope_status = 'IN_SCOPE'
        GROUP BY platform, author_handle
        HAVING COUNT(*) >= 1
    """
    params = [
        bigquery.ScalarQueryParameter("region", "STRING", region),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        authors_path = tmp_dir / "authors.jsonl"
        personas_path = tmp_dir / "personas.jsonl"

        n_authors = _extract_query_to_jsonl(client, query, params, authors_path)
        _log(f"extracted {n_authors} author records (lookback={lookback_days}d)")
        if n_authors == 0:
            _log("no authors — exiting 0")
            return 0

        cmd = [
            sys.executable,
            "-m",
            "mapear_nlp.graph.run_author_resolution",
            "--authors",
            str(authors_path),
            "--out",
            str(personas_path),
            "--region",
            region,
        ]
        _log(f"invoking: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)

        if not personas_path.exists() or personas_path.stat().st_size == 0:
            _log("no personas emitted — exiting 0")
            return 0

        table_id = f"{project}.{silver_ds}.silver_author_personas"
        n_loaded = _load_jsonl_to_bq(
            client, personas_path, table_id, "silver_author_personas"
        )
        _log(f"loaded {n_loaded} persona-member rows into {table_id}")
        return n_loaded


def _detect_communities(
    client: bigquery.Client,
    project: str,
    silver_ds: str,
    region: str,
    tenant_id: str | None,
) -> int:
    activations_table = f"`{project}.{silver_ds}.silver_author_activations`"
    personas_table = f"`{project}.{silver_ds}.silver_author_personas`"
    use_personas = _bool_env("MAPEAR_CIB_USE_PERSONAS", False)

    # The synchrony window is configured in days of lookback; default
    # window_hours is 24h so 2-day lookback covers a sliding window of
    # all pairs without losing the boundary case.
    lookback_days = int(os.environ.get("MAPEAR_GRAPH_LOOKBACK_DAYS", "2"))

    activations_query = f"""
        SELECT *
        FROM {activations_table}
        WHERE DATE(published_at)
              BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL @lookback_days DAY)
              AND CURRENT_DATE()
          AND region = @region
          AND (@tenant_id IS NULL OR tenant_id = @tenant_id)
    """
    activations_params = [
        bigquery.ScalarQueryParameter("region", "STRING", region),
        bigquery.ScalarQueryParameter("lookback_days", "INT64", lookback_days),
        bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
    ]

    personas_query = f"""
        SELECT platform, author_id, persona_id
        FROM {personas_table}
        WHERE region = @region
          AND (@tenant_id IS NULL OR tenant_id = @tenant_id)
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY platform, author_id ORDER BY run_at DESC
        ) = 1
    """
    personas_params = [
        bigquery.ScalarQueryParameter("region", "STRING", region),
        bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
    ]

    v3_scores_enabled = _bool_env("MAPEAR_CIB_V3_SCORES_ENABLED", False)
    v3_embeddings_enabled = _bool_env("MAPEAR_CIB_V3_EMBEDDINGS_ENABLED", False)

    # Embeddings query: load silver_social_post_embeddings for the same
    # lookback window so content_similarity is computed over recent posts.
    embeddings_table = f"`{project}.{silver_ds}.silver_social_post_embeddings`"
    embeddings_query = f"""
        SELECT content_hash, embedding
        FROM {embeddings_table}
        WHERE region = @region
          AND (@tenant_id IS NULL OR tenant_id = @tenant_id)
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY content_hash ORDER BY run_at DESC
        ) = 1
    """
    embeddings_params = [
        bigquery.ScalarQueryParameter("region", "STRING", region),
        bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        activations_path = tmp_dir / "activations.jsonl"
        personas_path = tmp_dir / "personas.jsonl"
        embeddings_path = tmp_dir / "embeddings.jsonl"
        communities_path = tmp_dir / "communities.jsonl"
        scores_path = tmp_dir / "community_scores.jsonl"
        series_path = tmp_dir / "cluster_series.jsonl"

        n_acts = _extract_query_to_jsonl(
            client, activations_query, activations_params, activations_path
        )
        _log(f"extracted {n_acts} activations (lookback={lookback_days}d)")
        if n_acts == 0:
            _log("no activations — exiting 0")
            return 0

        cmd = [
            sys.executable,
            "-m",
            "mapear_nlp.graph.run_community_detection",
            "--activations",
            str(activations_path),
            "--out",
            str(communities_path),
            "--region",
            region,
        ]

        if use_personas:
            n_personas = _extract_query_to_jsonl(
                client, personas_query, personas_params, personas_path
            )
            _log(f"extracted {n_personas} persona-member rows")
            if n_personas > 0:
                cmd.extend(["--personas", str(personas_path)])
            else:
                _log(
                    "MAPEAR_CIB_USE_PERSONAS=true but no personas in BQ — running v1 keys"
                )

        if v3_embeddings_enabled:
            n_embeddings = _extract_query_to_jsonl(
                client, embeddings_query, embeddings_params, embeddings_path
            )
            _log(f"extracted {n_embeddings} social post embeddings")
            if n_embeddings > 0:
                cmd.extend(["--embeddings", str(embeddings_path)])
            else:
                _log(
                    "MAPEAR_CIB_V3_EMBEDDINGS_ENABLED=true but no embeddings in BQ "
                    "— running without content_similarity"
                )

        if v3_scores_enabled:
            cmd.extend(["--scores-out", str(scores_path)])
            cmd.extend(["--series-out", str(series_path)])

        _log(f"invoking: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)

        if not communities_path.exists() or communities_path.stat().st_size == 0:
            _log("no communities emitted — exiting 0")
            return 0

        table_id = f"{project}.{silver_ds}.silver_author_communities"
        n_loaded = _load_jsonl_to_bq(
            client, communities_path, table_id, "silver_author_communities"
        )
        _log(f"loaded {n_loaded} community-member rows into {table_id}")

        if v3_scores_enabled:
            if scores_path.exists() and scores_path.stat().st_size > 0:
                scores_table = f"{project}.{silver_ds}.silver_community_scores"
                n_scores = _load_jsonl_to_bq(
                    client, scores_path, scores_table, "silver_community_scores"
                )
                _log(f"loaded {n_scores} community-score rows into {scores_table}")
            else:
                _log("v3 scores: no rows emitted (skipping load)")

            if series_path.exists() and series_path.stat().st_size > 0:
                series_table = f"{project}.{silver_ds}.silver_cluster_series"
                n_series = _load_jsonl_to_bq(
                    client, series_path, series_table, "silver_cluster_series"
                )
                _log(f"loaded {n_series} cluster-series rows into {series_table}")
            else:
                _log("v3 series: no rows emitted (skipping load)")

        return n_loaded


def main() -> int:
    mode = _required_env("GRAPH_JOB").strip().lower()
    project = _required_env("GCP_PROJECT_ID")
    silver_ds = _required_env("GCP_BQ_DATASET_SILVER")
    region = os.environ.get("MAPEAR_REGION", "rn").strip() or "rn"
    tenant_id = os.environ.get("MAPEAR_TENANT_ID") or None

    job_run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()
    _log(
        f"start mode={mode} project={project} region={region} "
        f"tenant={tenant_id or '-'} run_id={job_run_id} at={started_at}"
    )

    client = bigquery.Client(project=project)

    if mode == "resolve-personas":
        _resolve_personas(client, project, silver_ds, region, tenant_id)
    elif mode == "detect-communities":
        _detect_communities(client, project, silver_ds, region, tenant_id)
    else:
        raise SystemExit(
            f"unknown GRAPH_JOB={mode!r}; expected resolve-personas | detect-communities"
        )

    _log(f"done mode={mode} run_id={job_run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
