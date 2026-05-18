"""Cloud Run Job entrypoint for the Eixo 2 NLP jobs (v2a + v2b).

Two modes, selected by the ``NLP_JOB`` env var:

* ``cluster-narratives`` — Eixo 2 v2a. BQ ``gold_articles`` rows where
  ``narrative_summary IS NOT NULL`` are embedded with a sentence-transformer
  (content-addressed GCS cache) and clustered with HDBSCAN or
  cosine-threshold. Emits rows to ``silver_narrative_embeddings`` and
  ``silver_narrative_clusters``.

* ``classify-stances`` — Eixo 2 v2b. Same BQ extract (reuses the same
  ``narrative_summary IS NOT NULL`` filter). Each narrative is classified
  as favor/contra/neutro toward its target official via few-shot LLM
  (Anthropic Claude, GCS cache). Emits rows to ``silver_article_stances``.

This script is the *glue* layer: BQ extract → existing CLI → BQ load.
The CLIs themselves stay JSONL-pure so they remain trivially testable
without GCP credentials. WRITE_APPEND semantics + downstream dbt
incremental marts handle dedup.
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


SCHEMAS_DIR = Path(os.environ.get("NLP_RUNNER_SCHEMAS_DIR", "/app/schemas"))

# Gold articles fields consumed by both NLP jobs.
# gold_articles does not carry person_name / role — only person_id.
# The stance classifier accepts these as optional; they default to "".
_GOLD_QUERY = """
    SELECT
      content_hash,
      narrative_summary,
      narrative_prompt_version,
      rule_version,
      published_at,
      person_id,
      source_type,
      tenant_id,
      -- gold_articles ainda não carrega `region`; projeto é single-tenant RN.
      'rn' AS region
    FROM `{project}.{gold_ds}.gold_articles`
    WHERE narrative_summary IS NOT NULL
      AND (@tenant_id IS NULL OR tenant_id = @tenant_id)
"""


# Eixo 2 v2d — evidence for the mayor endorsement investigation.
# One row per (monitored mayor × event that co-mentions the mayor and at
# least one gubernatorial candidate). The orchestrator groups these into
# per-mayor bundles in Python before invoking the detector CLI.
#
# ASSUMPTION (validate against prod data): mentioned_persons carries the
# display names that match dim_rn_cities_mayors.mayor and dim_persons.name.
# If it carries ids or differently-formatted names the join yields empty
# bundles and every mayor gets an "Indefinido" verdict — degrade, not crash.
_ENDORSEMENT_BUNDLE_QUERY = """
    WITH mayors AS (
      SELECT mayor AS mayor_name, party AS mayor_party
      FROM `{project}.{gold_ds}.dim_rn_cities_mayors`
      WHERE is_current = TRUE AND state = 'RN'
        AND monitored = TRUE AND mayor IS NOT NULL
    ),
    candidates AS (
      SELECT DISTINCT name AS candidate
      FROM `{project}.{gold_ds}.dim_persons`
      WHERE is_current = TRUE
        AND role IN ('governor', 'governor_candidate')
    ),
    events AS (
      SELECT event_id, text, url, published_at, source_type, mentioned_persons
      FROM `{project}.{gold_ds}.mapear_events`
      WHERE rn_relevant = TRUE
        AND mentioned_persons IS NOT NULL
        AND text IS NOT NULL
        AND CAST(published_at AS DATE)
            >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
    ),
    -- BQ não aceita `JOIN ON a IN UNNEST(b)` nem `EXISTS ... IN UNNEST(b)`
    -- (vira LEFT SEMI JOIN sem equality) nem subquery correlacionada.
    -- Estratégia: pré-calcular o conjunto de event_ids que mencionam algum
    -- candidato, depois fazer JOIN explícito com mayors.
    events_with_candidate AS (
      SELECT DISTINCT e.event_id
      FROM events e
      CROSS JOIN UNNEST(e.mentioned_persons) AS p
      JOIN candidates c ON c.candidate = p
    ),
    events_with_mayor AS (
      SELECT
        e.event_id, e.text, e.url, e.published_at, e.source_type,
        m.mayor_name, m.mayor_party
      FROM events e
      CROSS JOIN UNNEST(e.mentioned_persons) AS mp
      JOIN mayors m ON m.mayor_name = mp
    )
    SELECT
      mayor_name,
      mayor_party,
      event_id,
      text,
      url,
      published_at,
      source_type
    FROM events_with_mayor
    WHERE event_id IN (SELECT event_id FROM events_with_candidate)
    ORDER BY mayor_name, published_at DESC
"""

# Full candidate roster — the prompt lists every option so the LLM picks.
_ENDORSEMENT_CANDIDATES_QUERY = """
    SELECT DISTINCT name AS candidate
    FROM `{project}.{gold_ds}.dim_persons`
    WHERE is_current = TRUE
      AND role IN ('governor', 'governor_candidate')
    ORDER BY candidate
"""

# Days of co-mention history fed to each investigation.
_ENDORSEMENT_WINDOW_DAYS = 90
# Max events per mayor bundle — most recent first (the detector also caps).
_ENDORSEMENT_MAX_EVENTS = 12


def _log(msg: str) -> None:
    sys.stderr.write(f"[nlp-runner] {msg}\n")
    sys.stderr.flush()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _load_schema(table: str) -> list[bigquery.SchemaField]:
    path = SCHEMAS_DIR / f"{table}.json"
    fields = json.loads(path.read_text(encoding="utf-8"))
    return [
        bigquery.SchemaField(f["name"], f["type"], mode=f.get("mode", "NULLABLE"))
        for f in fields
    ]


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _extract_gold_to_jsonl(
    client: bigquery.Client,
    project: str,
    gold_ds: str,
    region: str,
    tenant_id: str | None,
    out_path: Path,
) -> int:
    query = _GOLD_QUERY.format(project=project, gold_ds=gold_ds)
    params = [
        bigquery.ScalarQueryParameter("tenant_id", "STRING", tenant_id),
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


def _cluster_narratives(
    client: bigquery.Client,
    project: str,
    gold_ds: str,
    silver_ds: str,
    region: str,
    tenant_id: str | None,
) -> int:
    algorithm = os.environ.get("MAPEAR_EMBEDDINGS_CLUSTER_ALGORITHM", "hdbscan")
    gcs_bucket = os.environ.get("GCP_GCS_BUCKET_NAME", "")
    cache_prefix = os.environ.get(
        "MAPEAR_EMBEDDINGS_CACHE_GCS_PREFIX", "narrative_embeddings/"
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        gold_path = tmp_dir / "gold.jsonl"
        embeddings_path = tmp_dir / "embeddings.jsonl"
        clusters_path = tmp_dir / "clusters.jsonl"

        n_gold = _extract_gold_to_jsonl(
            client, project, gold_ds, region, tenant_id, gold_path
        )
        _log(f"extracted {n_gold} gold narratives")
        if n_gold == 0:
            _log("no narratives — exiting 0")
            return 0

        cmd = [
            sys.executable,
            "-m",
            "mapear_nlp.clustering.run_narrative_clustering",
            "--gold",
            str(gold_path),
            "--out-embeddings",
            str(embeddings_path),
            "--out-clusters",
            str(clusters_path),
            "--algorithm",
            algorithm,
            "--region",
            region,
            "--project-id",
            project,
        ]
        if gcs_bucket:
            cmd.extend(["--cache-bucket", gcs_bucket, "--cache-prefix", cache_prefix])
        else:
            cmd.append("--no-cache")
        _log(f"invoking: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)

        total = 0
        for out_path, table_name in [
            (embeddings_path, "silver_narrative_embeddings"),
            (clusters_path, "silver_narrative_clusters"),
        ]:
            if not out_path.exists() or out_path.stat().st_size == 0:
                _log(f"{table_name}: no output — skipping BQ load")
                continue
            table_id = f"{project}.{silver_ds}.{table_name}"
            n_loaded = _load_jsonl_to_bq(client, out_path, table_id, table_name)
            _log(f"loaded {n_loaded} rows into {table_id}")
            total += n_loaded

    return total


def _classify_stances(
    client: bigquery.Client,
    project: str,
    gold_ds: str,
    silver_ds: str,
    region: str,
    tenant_id: str | None,
) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        gold_path = tmp_dir / "gold.jsonl"
        stances_path = tmp_dir / "stances.jsonl"

        n_gold = _extract_gold_to_jsonl(
            client, project, gold_ds, region, tenant_id, gold_path
        )
        _log(f"extracted {n_gold} gold narratives")
        if n_gold == 0:
            _log("no narratives — exiting 0")
            return 0

        cmd = [
            sys.executable,
            "-m",
            "mapear_nlp.run_stance_classification",
            "--gold",
            str(gold_path),
            "--out",
            str(stances_path),
            "--region",
            region,
        ]
        _log(f"invoking: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)

        if not stances_path.exists() or stances_path.stat().st_size == 0:
            _log("no stances emitted — exiting 0")
            return 0

        table_id = f"{project}.{silver_ds}.silver_article_stances"
        n_loaded = _load_jsonl_to_bq(
            client, stances_path, table_id, "silver_article_stances"
        )
        _log(f"loaded {n_loaded} stance rows into {table_id}")
        return n_loaded


def _investigate_endorsements(
    client: bigquery.Client,
    project: str,
    gold_ds: str,
    silver_ds: str,
    region: str,
    tenant_id: str | None,
) -> int:
    """Eixo 2 v2d — LLM mayor endorsement investigation.

    Builds per-mayor evidence bundles from ``mapear_events``, invokes the
    detector CLI, and appends verdicts to ``silver_mayor_endorsements``.
    """
    cand_rows = client.query(
        _ENDORSEMENT_CANDIDATES_QUERY.format(project=project, gold_ds=gold_ds)
    ).result()
    candidates = [r["candidate"] for r in cand_rows if r["candidate"]]
    _log(f"candidate roster ({len(candidates)}): {candidates}")
    if not candidates:
        _log("no gubernatorial candidates in dim_persons — exiting 0")
        return 0

    query = _ENDORSEMENT_BUNDLE_QUERY.format(project=project, gold_ds=gold_ds)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("days", "INT64", _ENDORSEMENT_WINDOW_DAYS)
        ]
    )
    rows = client.query(query, job_config=job_config).result()

    # Group co-mention events into per-mayor bundles (recent first, capped).
    bundles: dict[str, dict] = {}
    for row in rows:
        name = row["mayor_name"]
        bundle = bundles.get(name)
        if bundle is None:
            bundle = {
                "mayor_id": name,
                "mayor_name": name,
                "mayor_party": row["mayor_party"] or "",
                "candidates": candidates,
                "region": region,
                "tenant_id": tenant_id,
                "articles": [],
            }
            bundles[name] = bundle
        if len(bundle["articles"]) >= _ENDORSEMENT_MAX_EVENTS:
            continue
        bundle["articles"].append(
            {
                "article_id": row["event_id"],
                "title": "",
                "text": row["text"] or "",
                "published_at": _json_safe(row["published_at"]),
                "source": row["source_type"] or "",
            }
        )

    if not bundles:
        _log("no mayor co-mention bundles in window — exiting 0")
        return 0
    _log(f"built {len(bundles)} mayor evidence bundles")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        mayors_path = tmp_dir / "mayor_bundles.jsonl"
        out_path = tmp_dir / "endorsements.jsonl"

        with mayors_path.open("w", encoding="utf-8") as fh:
            for bundle in bundles.values():
                fh.write(json.dumps(bundle, ensure_ascii=False) + "\n")

        cmd = [
            sys.executable,
            "-m",
            "mapear_nlp.run_mayor_endorsement_detection",
            "--mayors",
            str(mayors_path),
            "--out",
            str(out_path),
            "--region",
            region,
        ]
        _log(f"invoking: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)

        if not out_path.exists() or out_path.stat().st_size == 0:
            _log("no endorsements emitted — exiting 0")
            return 0

        table_id = f"{project}.{silver_ds}.silver_mayor_endorsements"
        n_loaded = _load_jsonl_to_bq(
            client, out_path, table_id, "silver_mayor_endorsements"
        )
        _log(f"loaded {n_loaded} endorsement rows into {table_id}")
        return n_loaded


def main() -> int:
    mode = _required_env("NLP_JOB").strip().lower()
    project = _required_env("GCP_PROJECT_ID")
    gold_ds = _required_env("GCP_BQ_DATASET_GOLD")
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

    if mode == "cluster-narratives":
        _cluster_narratives(client, project, gold_ds, silver_ds, region, tenant_id)
    elif mode == "classify-stances":
        _classify_stances(client, project, gold_ds, silver_ds, region, tenant_id)
    elif mode == "investigate-endorsements":
        _investigate_endorsements(
            client, project, gold_ds, silver_ds, region, tenant_id
        )
    else:
        raise SystemExit(
            f"unknown NLP_JOB={mode!r}; expected cluster-narratives | "
            "classify-stances | investigate-endorsements"
        )

    _log(f"done mode={mode} run_id={job_run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
