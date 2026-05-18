"""Out-of-band narrative-clustering job — Eixo 2 v2a.

Designed to run as a Cloud Run Job on a daily cadence. Reads a JSONL
batch of ``GoldArticle`` rows where ``narrative_summary IS NOT NULL``,
groups by ``(cluster_run_date, region)``, embeds each narrative (with
content-addressed cache), runs the configured clustering algorithm,
and emits two JSONL streams:

  * ``--out-embeddings`` — one row per narrative (the
    ``silver_narrative_embeddings`` shape).
  * ``--out-clusters`` — one row per (narrative, cluster assignment)
    (the ``silver_narrative_clusters`` shape, with outliers as
    ``cluster_id = -1``).

Why two streams
---------------
Embeddings are content-addressed (re-running tomorrow on the same
narrative is a cache hit and the embedding row is a no-op MERGE).
Clusters are date-partitioned (cluster IDs are not stable across
days). Splitting them lets the loader use different MERGE strategies
without bloating either schema.

Why out-of-band, not inline in the RSS pipeline
-----------------------------------------------
* The clustering compute scales with N² in the cosine_threshold path
  (and N log N in HDBSCAN). Cheap per day; expensive per batch.
* Cluster IDs need the full day's narratives to be meaningful. A per-
  batch run would emit unstable IDs that the operator cannot act on.
* Separating concerns: the RSS pipeline keeps its narrative-summary
  hot path (Stage 4.6 LLM call) without taking on a model-load cost.

Inputs
------
JSONL of GoldArticle rows. The orchestrator (Cloud Scheduler →
Cloud Run Job) is expected to ``bq extract`` ``mapear_gold.gold_articles``
into GCS just before invoking; the script does not query BigQuery
directly to keep this module dependency-light. Required fields per row:
``content_hash``, ``narrative_summary``, ``published_at``,
``narrative_prompt_version``, ``rule_version``, ``region``, ``tenant_id``.

Outputs
-------
Two JSONL files (or stdout for one of them, with the other required).
Downstream loader picks up the files and MERGEs into
``mapear_silver.silver_narrative_embeddings`` and
``mapear_silver.silver_narrative_clusters``.

Local invocation
----------------

::

    poetry run python -m mapear_nlp.clustering.run_narrative_clustering \\
        --gold /tmp/gold_articles.jsonl \\
        --out-embeddings /tmp/embeddings.jsonl \\
        --out-clusters /tmp/clusters.jsonl \\
        --algorithm hdbscan \\
        --region rn

The Makefile target ``make cluster-narratives`` wires this up with the
defaults read from ``MAPEAR_EMBEDDINGS_*`` settings.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from mapear_infra.config import get_settings

from mapear_nlp.clustering.narrative import compute_narrative_clusters
from mapear_nlp.embeddings.cache import EmbeddingCache
from mapear_nlp.embeddings.client import EmbeddingClient, get_embedding_client
from mapear_nlp.embeddings.encoder import CacheAwareEncoder


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _load_gold(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file of GoldArticle rows.

    Filters to rows with a non-empty ``narrative_summary``. Pre-Eixo-2-v1
    rows and WARNING/FAVORABLE rows have ``narrative_summary IS NULL``
    and are silently skipped — they have nothing to cluster.
    """
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("narrative_summary"):
                continue
            if isinstance(row.get("published_at"), str):
                row["published_at"] = _parse_iso(row["published_at"])
            rows.append(row)
    return rows


def _group_by_day_region(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[date, str | None], list[dict[str, Any]]]:
    groups: dict[tuple[date, str | None], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        published = row.get("published_at")
        if published is None:
            # Defensive: GoldArticle.published_at is Optional in the
            # Pydantic model. Rows without a date land in a "no_date"
            # bucket so they are still emitted; analysts can filter.
            day = datetime.now(UTC).date()
        else:
            day = published.astimezone(UTC).date()
        groups[(day, row.get("region"))].append(row)
    return groups


def _build_encoder(
    *,
    embedding_client: EmbeddingClient | None,
    cache_bucket: str | None,
    cache_prefix: str,
    project_id: str,
    cache_enabled: bool,
) -> CacheAwareEncoder:
    if embedding_client is None:
        embedding_client = get_embedding_client(get_settings().embeddings)
    cache: EmbeddingCache | None = None
    if cache_enabled and cache_bucket:
        cache = EmbeddingCache.build(
            bucket_name=cache_bucket,
            project_id=project_id,
            prefix=cache_prefix,
        )
    return CacheAwareEncoder(client=embedding_client, cache=cache)


def run(
    gold_path: Path,
    embeddings_out: Path,
    clusters_out: Path,
    *,
    algorithm: str,
    min_size: int,
    cosine_threshold: float,
    region_filter: str | None,
    pipeline_version: str,
    embedding_client: EmbeddingClient | None = None,
    cache_bucket: str | None = None,
    cache_prefix: str = "narrative_embeddings/",
    project_id: str = "",
    cache_enabled: bool = True,
) -> int:
    rows = _load_gold(gold_path)
    if region_filter is not None:
        rows = [r for r in rows if r.get("region") == region_filter]
    if not rows:
        sys.stderr.write("no narratives after filter — nothing to do\n")
        return 0

    encoder = _build_encoder(
        embedding_client=embedding_client,
        cache_bucket=cache_bucket,
        cache_prefix=cache_prefix,
        project_id=project_id,
        cache_enabled=cache_enabled,
    )

    groups = _group_by_day_region(rows)
    job_run_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    embedding_rows: list[dict[str, Any]] = []
    cluster_rows: list[dict[str, Any]] = []

    for (day, region), grouped in groups.items():
        items = [(r["content_hash"], r["narrative_summary"]) for r in grouped]
        encode_result = encoder.encode_with_hashes(items)
        sys.stderr.write(
            f"group ({day}, {region}): {len(items)} narratives "
            f"({encode_result.cache_hits} cache hits, "
            f"{encode_result.encoded} encoded)\n"
        )

        # Emit one embedding row per narrative — content-addressed, so
        # the same content_hash + model is idempotent under MERGE.
        for r, vec in zip(grouped, encode_result.vectors, strict=True):
            embedding_rows.append(
                {
                    "content_hash": r["content_hash"],
                    "embedding_model": encoder.model,
                    "embedding_dim": encoder.dim,
                    "embedding": vec,
                    "narrative_prompt_version": r.get("narrative_prompt_version"),
                    "rule_version": r.get("rule_version"),
                    "job_run_id": job_run_id,
                    "run_at": now.isoformat(),
                    "pipeline_version": pipeline_version,
                    "schema_version": 1,
                    "source_type": r.get("source_type", "rss"),
                    "region": region,
                    "tenant_id": r.get("tenant_id"),
                }
            )

        # Cluster within this (day, region). Outliers are emitted with
        # cluster_id=-1 so the analyst can see lonely narratives.
        clustering = compute_narrative_clusters(
            list(
                zip(
                    [r["content_hash"] for r in grouped],
                    encode_result.vectors,
                    strict=True,
                )
            ),
            algorithm=algorithm,  # type: ignore[arg-type]
            min_size=min_size,
            cosine_threshold=cosine_threshold,
        )
        # Pre-index cluster stats by cluster_id for the assignment loop.
        cluster_stats = {c.cluster_id: c for c in clustering.clusters}

        day_dt = datetime(day.year, day.month, day.day, tzinfo=UTC)
        # We need tenant_id per row, so build a hash-indexed lookup.
        row_by_hash = {r["content_hash"]: r for r in grouped}
        for assignment in clustering.assignments:
            stats = cluster_stats.get(assignment.cluster_id)
            source_row = row_by_hash[assignment.content_hash]
            cluster_rows.append(
                {
                    "cluster_run_date": day_dt.isoformat(),
                    "region": region,
                    "algorithm": algorithm,
                    "content_hash": assignment.content_hash,
                    "embedding_model": encoder.model,
                    "cluster_id": assignment.cluster_id,
                    "member_role": assignment.member_role,
                    "cluster_size": stats.cluster_size if stats else 1,
                    "distance_to_centroid": assignment.distance_to_centroid,
                    "avg_intra_cluster_distance": (
                        stats.avg_intra_cluster_distance if stats else None
                    ),
                    "cluster_label": None,
                    "job_run_id": job_run_id,
                    "run_at": now.isoformat(),
                    "pipeline_version": pipeline_version,
                    "schema_version": 1,
                    "source_type": source_row.get("source_type", "rss"),
                    "tenant_id": source_row.get("tenant_id"),
                }
            )

    with embeddings_out.open("w", encoding="utf-8") as fh:
        for row in embedding_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with clusters_out.open("w", encoding="utf-8") as fh:
        for row in cluster_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    sys.stderr.write(
        f"emitted {len(embedding_rows)} embedding rows + "
        f"{len(cluster_rows)} cluster rows across {len(groups)} "
        f"(date, region) groups\n"
    )
    return 0


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Cluster narratives over sentence embeddings (Eixo 2 v2a)"
    )
    parser.add_argument(
        "--gold",
        type=Path,
        required=True,
        help="JSONL with GoldArticle rows (filtered to narrative_summary IS NOT NULL)",
    )
    parser.add_argument(
        "--out-embeddings",
        type=Path,
        required=True,
        help="Output JSONL path for silver_narrative_embeddings rows",
    )
    parser.add_argument(
        "--out-clusters",
        type=Path,
        required=True,
        help="Output JSONL path for silver_narrative_clusters rows",
    )
    parser.add_argument(
        "--algorithm",
        default=settings.embeddings.cluster_algorithm,
        choices=["hdbscan", "cosine_threshold"],
    )
    parser.add_argument(
        "--min-size", type=int, default=settings.embeddings.cluster_min_size
    )
    parser.add_argument(
        "--cosine-threshold",
        type=float,
        default=settings.embeddings.cluster_cosine_threshold,
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Filter narratives to this region slug only.",
    )
    parser.add_argument("--pipeline-version", default="0.1.0")
    parser.add_argument(
        "--cache-bucket",
        default=settings.gcp.gcs_bucket_name or None,
        help="GCS bucket for the embedding cache. Empty disables the cache.",
    )
    parser.add_argument(
        "--cache-prefix",
        default=settings.embeddings.cache_gcs_prefix,
    )
    parser.add_argument(
        "--project-id",
        default=settings.gcp.project_id,
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the embedding cache (re-encode every narrative).",
    )
    args = parser.parse_args()

    return run(
        gold_path=args.gold,
        embeddings_out=args.out_embeddings,
        clusters_out=args.out_clusters,
        algorithm=args.algorithm,
        min_size=args.min_size,
        cosine_threshold=args.cosine_threshold,
        region_filter=args.region,
        pipeline_version=args.pipeline_version,
        cache_bucket=args.cache_bucket,
        cache_prefix=args.cache_prefix,
        project_id=args.project_id,
        cache_enabled=not args.no_cache and settings.embeddings.cache_enabled,
    )


if __name__ == "__main__":
    sys.exit(main())
