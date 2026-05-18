"""Out-of-band community-detection job — Eixo 3 v2a + v3.

Designed to run as a Cloud Run Job on a daily cadence. Reads
``silver_author_activations`` rows for a date range, groups by
(activation_date, region), runs the v1 co-activation engine + the
v2a community detection on each group, and emits one
``SilverAuthorCommunity`` row per (date, region, author, algorithm).

Eixo 3 v3 extensions:
  * ``--scores-out`` — also emits one ``SilverCommunityScore`` row per
    (date, region, algorithm, community_id).
  * ``--series-out`` — also emits one ``SilverClusterSeries`` row per
    (date, region, algorithm, community_id). Cross-day matching uses all
    dates present in the input activation window; the orchestrator
    should supply a rolling window (e.g. last 7 days) so continuations
    are tracked.

Why out-of-band, not in the per-batch social pipeline
-----------------------------------------------------
* The graph compute is O(E log E) for Louvain — fine for daily, too
  expensive per batch (every 8h).
* It needs the full day's activations, so it cannot run on incomplete
  batches without paying the cost N times per day.
* Community IDs are not stable across days; doing it in the hot path
  would force a re-numbering scheme that v2a deliberately defers to v3.

Inputs
------
The script reads a JSONL or Parquet activation export. The orchestrator
(Cloud Scheduler → Cloud Run Job) is expected to run a ``bq extract``
into GCS just before invoking; the script does not query BigQuery
directly to keep this module dependency-light.

Outputs
-------
JSONL on stdout by default; ``--out path.jsonl`` writes to disk. The
downstream loader (``mapear_storage.loaders.bq_loader``) picks up the
file and MERGEs into ``mapear_silver.silver_author_communities``.
``--scores-out`` and ``--series-out`` work analogously.

Local invocation
----------------

::

    poetry run python -m mapear_nlp.graph.run_community_detection \\
        --activations /tmp/activations.jsonl \\
        --out /tmp/communities.jsonl \\
        --scores-out /tmp/community_scores.jsonl \\
        --series-out /tmp/cluster_series.jsonl \\
        --algorithm louvain \\
        --region rn

The Makefile target ``make detect-communities`` wires this up with the
defaults read from ``MAPEAR_CIB_COMMUNITY_*`` settings.
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

from mapear_nlp.graph.cluster_tracker import SeriesAssignment, track_cluster_series
from mapear_nlp.graph.coactivation import PersonaLookup, compute_coactivation_scores
from mapear_nlp.graph.community import CommunityStats, build_graph, detect_communities
from mapear_nlp.graph.scoring import ScoringWeights, score_communities


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _load_persona_lookup(path: Path) -> PersonaLookup:
    """Read silver_author_personas JSONL into a (platform, author_id) → persona_id map.

    Eixo 3 v2b plug — opt-in via ``--personas`` (or the
    ``MAPEAR_CIB_USE_PERSONAS`` flag at the orchestrator level). When
    absent, the community job runs on the v1 surrogate
    ``(platform, author_id)`` keys unchanged.
    """
    lookup: dict[tuple[str, str], str] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            lookup[(str(row["platform"]), str(row["author_id"]))] = str(
                row["persona_id"]
            )
    return lookup


def _load_embeddings(path: Path) -> dict[str, list[float]]:
    """Read a JSONL file of silver_social_post_embeddings rows.

    Returns a ``content_hash → embedding`` dict for fast lookup in
    ``compute_coactivation_scores``. When a content_hash appears more
    than once (two model vintages), the last row wins — the caller
    controls which model is queried upstream.

    Eixo 2 v2a social — opt-in via ``--embeddings`` (or the
    ``MAPEAR_CIB_V3_EMBEDDINGS_ENABLED`` env flag at the orchestrator
    level). When absent the job runs with ``content_embeddings=None``
    and ``avg_content_similarity`` is None in all output pairs.
    """
    lookup: dict[str, list[float]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            h = str(row["content_hash"])
            vec = row.get("embedding")
            if isinstance(vec, list):
                lookup[h] = [float(x) for x in vec]
    return lookup


def _load_activations(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file of activation rows.

    Parquet support is intentionally not bundled here — the orchestrator
    converts to JSONL via ``bq extract --destination_format=NEWLINE_DELIMITED_JSON``.
    Keeping the script JSONL-only avoids dragging pyarrow into mapear-nlp
    (it already pulls pyarrow transitively through mapear-storage, but
    this script must run in a slim image that may not have storage).
    """
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row["published_at"], str):
                row["published_at"] = _parse_iso(row["published_at"])
            rows.append(row)
    return rows


def _group_by_day_region(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[date, str | None], list[dict[str, Any]]]:
    groups: dict[tuple[date, str | None], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day = row["published_at"].astimezone(UTC).date()
        groups[(day, row.get("region"))].append(row)
    return groups


def _emit_community_rows(
    communities: list[CommunityStats],
    day_dt: datetime,
    region: str | None,
    algorithm: str,
    job_run_id: str,
    now: datetime,
    pipeline_version: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in communities:
        for member in c.members:
            out.append(
                {
                    "activation_date": day_dt.isoformat(),
                    "region": region,
                    "author_id": member.author_id,
                    "platform": member.platform,
                    "algorithm": algorithm,
                    "community_id": c.community_id,
                    "community_size": len(c.members),
                    "edge_count": c.edge_count,
                    "edge_density": c.edge_density,
                    "avg_co_post_count": c.avg_co_post_count,
                    "avg_jaccard": c.avg_jaccard,
                    "job_run_id": job_run_id,
                    "run_at": now.isoformat(),
                    "pipeline_version": pipeline_version,
                    "schema_version": 1,
                    "source_type": "social",
                    "tenant_id": None,
                }
            )
    return out


def _emit_score_rows(
    communities: list[CommunityStats],
    pairs: Any,
    day_dt: datetime,
    region: str | None,
    algorithm: str,
    weights: ScoringWeights,
    sync_cap: float,
    job_run_id: str,
    now: datetime,
    pipeline_version: str,
) -> list[dict[str, Any]]:
    community_scores = score_communities(communities, pairs, weights, sync_cap=sync_cap)
    weights_json = json.dumps(
        {
            "synchrony": weights.synchrony,
            "alignment": weights.alignment,
            "content_similarity": weights.content_similarity,
        }
    )
    out: list[dict[str, Any]] = []
    for cs in community_scores:
        out.append(
            {
                "activation_date": day_dt.isoformat(),
                "region": region,
                "algorithm": algorithm,
                "community_id": cs.community_id,
                "community_size": cs.member_count,
                "pair_count": cs.pair_count,
                "avg_synchrony_score": cs.avg_synchrony,
                "avg_alignment_score": cs.avg_alignment,
                "avg_content_similarity_score": cs.avg_content_similarity,
                "composite_score": cs.composite_score,
                "score_version": "v1",
                "score_weights_json": weights_json,
                "job_run_id": job_run_id,
                "run_at": now.isoformat(),
                "pipeline_version": pipeline_version,
                "schema_version": 1,
                "source_type": "social",
                "tenant_id": None,
            }
        )
    return out


def _emit_series_rows(
    series_assignments: list[SeriesAssignment],
    region: str | None,
    algorithm: str,
    job_run_id: str,
    now: datetime,
    pipeline_version: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sa in series_assignments:
        day_dt = datetime(
            sa.activation_date.year,
            sa.activation_date.month,
            sa.activation_date.day,
            tzinfo=UTC,
        )
        start_dt = datetime(
            sa.series_start_date.year,
            sa.series_start_date.month,
            sa.series_start_date.day,
            tzinfo=UTC,
        )
        out.append(
            {
                "activation_date": day_dt.isoformat(),
                "region": region,
                "algorithm": algorithm,
                "community_id": sa.community_id,
                "series_id": sa.series_id,
                "series_start_date": start_dt.isoformat(),
                "jaccard_to_previous": sa.jaccard_to_previous,
                "is_new_series": sa.is_new_series,
                "job_run_id": job_run_id,
                "run_at": now.isoformat(),
                "pipeline_version": pipeline_version,
                "schema_version": 1,
                "source_type": "social",
                "tenant_id": None,
            }
        )
    return out


def _write_jsonl(rows: list[dict[str, Any]], path: Path | None) -> None:
    fh = path.open("w", encoding="utf-8") if path else sys.stdout
    try:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        if path:
            fh.close()


def run(
    activations_path: Path,
    out_path: Path | None,
    *,
    algorithm: str,
    resolution: float,
    seed: int,
    min_size: int,
    window_hours: float,
    min_overlap: int,
    region_filter: str | None,
    pipeline_version: str,
    persona_lookup: PersonaLookup | None = None,
    content_embeddings: dict[str, list[float]] | None = None,
    scores_out: Path | None = None,
    series_out: Path | None = None,
    score_weights: ScoringWeights | None = None,
    sync_cap: float = 20.0,
    cluster_series_threshold: float = 0.5,
) -> int:
    activations = _load_activations(activations_path)
    if region_filter is not None:
        activations = [a for a in activations if a.get("region") == region_filter]
    if not activations:
        sys.stderr.write("no activations after filter — nothing to do\n")
        return 0

    groups = _group_by_day_region(activations)
    job_run_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    if score_weights is None:
        score_weights = ScoringWeights()

    community_rows: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []

    # Accumulate communities per (region, date) for cross-day series tracking.
    # Structure: region → { date → list[CommunityStats] }
    communities_by_region: dict[str | None, dict[date, list[CommunityStats]]] = (
        defaultdict(dict)
    )

    for (day, region), rows in sorted(groups.items()):
        pairs = compute_coactivation_scores(
            rows,
            window_hours=window_hours,
            min_overlap=min_overlap,
            persona_lookup=persona_lookup,
            content_embeddings=content_embeddings,
        )
        if not pairs:
            continue
        graph = build_graph(pairs)
        communities = detect_communities(
            graph,
            algorithm=algorithm,  # type: ignore[arg-type]
            resolution=resolution,
            seed=seed,
            min_size=min_size,
        )
        day_dt = datetime(day.year, day.month, day.day, tzinfo=UTC)

        community_rows.extend(
            _emit_community_rows(
                communities,
                day_dt,
                region,
                algorithm,
                job_run_id,
                now,
                pipeline_version,
            )
        )
        if scores_out is not None:
            score_rows.extend(
                _emit_score_rows(
                    communities,
                    pairs,
                    day_dt,
                    region,
                    algorithm,
                    score_weights,
                    sync_cap,
                    job_run_id,
                    now,
                    pipeline_version,
                )
            )
        if series_out is not None:
            communities_by_region[region][day] = communities

    _write_jsonl(community_rows, out_path)

    if scores_out is not None:
        _write_jsonl(score_rows, scores_out)
        sys.stderr.write(f"emitted {len(score_rows)} community score rows\n")

    if series_out is not None:
        series_rows: list[dict[str, Any]] = []
        for region, days_map in communities_by_region.items():
            assignments = track_cluster_series(
                days_map, threshold=cluster_series_threshold
            )
            series_rows.extend(
                _emit_series_rows(
                    assignments, region, algorithm, job_run_id, now, pipeline_version
                )
            )
        _write_jsonl(series_rows, series_out)
        sys.stderr.write(f"emitted {len(series_rows)} cluster series rows\n")

    sys.stderr.write(
        f"emitted {len(community_rows)} community-member rows across "
        f"{len(groups)} (date, region) groups\n"
    )
    return 0


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Detect author communities (Eixo 3 v2a + v3)"
    )
    parser.add_argument(
        "--activations",
        type=Path,
        required=True,
        help="JSONL with silver_author_activations rows",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSONL path for silver_author_communities; default stdout",
    )
    parser.add_argument(
        "--scores-out",
        type=Path,
        default=None,
        dest="scores_out",
        help="Optional output JSONL for silver_community_scores (Eixo 3 v3)",
    )
    parser.add_argument(
        "--series-out",
        type=Path,
        default=None,
        dest="series_out",
        help="Optional output JSONL for silver_cluster_series (Eixo 3 v3)",
    )
    parser.add_argument(
        "--algorithm",
        default=settings.cib.community_algorithm,
        choices=["louvain", "label_propagation"],
    )
    parser.add_argument(
        "--resolution", type=float, default=settings.cib.community_resolution
    )
    parser.add_argument("--seed", type=int, default=settings.cib.community_seed)
    parser.add_argument("--min-size", type=int, default=settings.cib.community_min_size)
    parser.add_argument("--window-hours", type=float, default=settings.cib.window_hours)
    parser.add_argument("--min-overlap", type=int, default=settings.cib.min_overlap)
    parser.add_argument(
        "--region",
        default=None,
        help="Filter activations to this region slug only.",
    )
    parser.add_argument("--pipeline-version", default="0.1.0")
    parser.add_argument(
        "--personas",
        type=Path,
        default=None,
        help=(
            "Optional JSONL of silver_author_personas rows (Eixo 3 v2b). "
            "When provided the engine keys nodes by persona_id; otherwise "
            "v1 surrogate (platform, author_id) is used."
        ),
    )
    parser.add_argument(
        "--embeddings",
        type=Path,
        default=None,
        help=(
            "Optional JSONL of silver_social_post_embeddings rows (Eixo 2 v2a social). "
            "When provided each AuthorPair receives avg_content_similarity; "
            "otherwise the field is None and the Eixo 3 v3 composite score "
            "redistributes weights over synchrony + alignment."
        ),
    )
    parser.add_argument(
        "--sync-cap",
        type=float,
        default=settings.cib.score_sync_cap,
        dest="sync_cap",
        help="co_post_count value that saturates synchrony to 1.0 (v3 scoring).",
    )
    parser.add_argument(
        "--series-threshold",
        type=float,
        default=settings.cib.cluster_series_threshold,
        dest="series_threshold",
        help="Minimum Jaccard to continue a cluster series across days (v3).",
    )
    args = parser.parse_args()

    persona_lookup: PersonaLookup | None = None
    if args.personas is not None:
        persona_lookup = _load_persona_lookup(args.personas)

    content_embeddings: dict[str, list[float]] | None = None
    if args.embeddings is not None:
        content_embeddings = _load_embeddings(args.embeddings)
        sys.stderr.write(f"loaded {len(content_embeddings)} social post embeddings\n")

    weights = ScoringWeights(
        synchrony=settings.cib.score_sync_weight,
        alignment=settings.cib.score_jaccard_weight,
        content_similarity=settings.cib.score_content_sim_weight,
    )

    return run(
        activations_path=args.activations,
        out_path=args.out,
        algorithm=args.algorithm,
        resolution=args.resolution,
        seed=args.seed,
        min_size=args.min_size,
        window_hours=args.window_hours,
        min_overlap=args.min_overlap,
        region_filter=args.region,
        pipeline_version=args.pipeline_version,
        persona_lookup=persona_lookup,
        content_embeddings=content_embeddings,
        scores_out=args.scores_out,
        series_out=args.series_out,
        score_weights=weights,
        sync_cap=args.sync_cap,
        cluster_series_threshold=args.series_threshold,
    )


if __name__ == "__main__":
    sys.exit(main())
