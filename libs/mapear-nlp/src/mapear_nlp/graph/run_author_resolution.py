"""Out-of-band cross-platform author-resolution job — Eixo 3 v2b.

Reads a JSONL of *author records* — one row per ``(platform,
author_id)`` with optional ``display_name``, ``verified``,
``base_city``, and an aggregated ``content_hashes`` list — and emits a
JSONL of ``silver_author_personas`` rows (one row per persona member).

Why out-of-band, not in the per-batch social pipeline
-----------------------------------------------------
Same reasons as the community-detection job (v2a):

* Blocking + pairwise compares everyone within a bucket; cheap daily,
  wasteful per 8h batch.
* Persona stability is a *daily* property — the engine is determ-
  inistic over a given input but adding tomorrow's accounts can
  renumber ``persona_id``. Pushing this into the hot path forces a
  renumbering scheme that v3 will own.
* The persona set is consumed by the v2a community job (optionally,
  via ``MAPEAR_CIB_USE_PERSONAS``) — running this *before* community
  detection lets the graph see one node per persona instead of one
  per ``(platform, author_id)``.

Inputs
------
JSONL where each line is a row with at minimum ``platform`` and
``author_id``. Optional: ``display_name``, ``verified``, ``base_city``,
``content_hashes`` (list[str]), ``region``, ``tenant_id``. The
orchestrator (Cloud Scheduler → Cloud Run Job) is expected to
materialise this from ``silver_social_posts`` via a GROUP BY in BQ
followed by ``bq extract --destination_format=NEWLINE_DELIMITED_JSON``.

Outputs
-------
JSONL on stdout by default; ``--out path.jsonl`` writes to disk. One
row per persona member (so a 3-platform persona produces 3 rows). The
downstream BQ loader MERGEs into ``mapear_silver.silver_author_personas``
on ``(persona_id, platform, author_id)``.

Local invocation
----------------

::

    poetry run python -m mapear_nlp.graph.run_author_resolution \\
        --authors /tmp/authors.jsonl \\
        --out /tmp/personas.jsonl \\
        --region rn

The Makefile target ``make resolve-personas`` wires this up with the
defaults read from ``MAPEAR_CIB_ER_*`` settings.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mapear_domain.entity_resolution.author_resolver import (
    IDENTITY_RESOLUTION_AUTHOR_VERSION,
    Persona,
    Thresholds,
    resolve_personas,
)
from mapear_infra.audit import log_persona_resolution
from mapear_infra.config import get_settings


def _load_authors(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _group_by_region(
    rows: Iterable[dict[str, Any]],
) -> dict[str | None, list[dict[str, Any]]]:
    groups: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row.get("region")].append(row)
    return groups


def _evidence_payload(persona: Persona) -> str:
    """Serialise PairScore evidence to a JSON string for BQ.

    Compact form — analysts who need it can ``JSON_EXTRACT`` columns.
    Avoids dragging a nested STRUCT array through the contract.
    """
    return json.dumps(
        [
            {
                "handle_similarity": round(s.handle_similarity, 4),
                "display_name_similarity": (
                    round(s.display_name_similarity, 4)
                    if s.display_name_similarity is not None
                    else None
                ),
                "verified_agreement": s.verified_agreement,
                "content_hash_overlap": s.content_hash_overlap,
                "city_match": s.city_match,
                "decision": s.decision,
                "confidence": round(s.confidence, 4),
            }
            for s in persona.evidence
        ],
        ensure_ascii=False,
    )


def run(
    authors_path: Path,
    out_path: Path | None,
    *,
    handle_similarity: float,
    display_name_similarity: float,
    min_shared_content: int,
    use_content_hash_bridge: bool,
    region_filter: str | None,
    pipeline_version: str,
    audit_enabled: bool,
) -> int:
    authors = _load_authors(authors_path)
    if region_filter is not None:
        authors = [a for a in authors if a.get("region") == region_filter]
    if not authors:
        sys.stderr.write("no authors after filter — nothing to do\n")
        return 0

    thresholds = Thresholds(
        handle_similarity=handle_similarity,
        display_name_similarity=display_name_similarity,
        min_shared_content=min_shared_content,
        use_content_hash_bridge=use_content_hash_bridge,
    )

    groups = _group_by_region(authors)
    job_run_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    day_dt = datetime(now.year, now.month, now.day, tzinfo=UTC)
    activation_date_iso = day_dt.isoformat()
    run_at_iso = now.isoformat()

    out_rows: list[dict[str, Any]] = []
    for region, rows in groups.items():
        personas = resolve_personas(rows, thresholds=thresholds)
        if not personas:
            continue
        # Tenant carry — assume single-tenant per region in v2b. If a
        # batch mixes tenants in one region the first wins; multi-tenant
        # ER is a v3 concern (we'd need to block within tenant, not
        # across).
        tenant_id = rows[0].get("tenant_id") if rows else None
        evidence_payload_by_persona = {
            p.persona_id: _evidence_payload(p) for p in personas
        }
        for persona in personas:
            if audit_enabled:
                log_persona_resolution(
                    tenant_id=tenant_id,
                    region=region,
                    persona_id=persona.persona_id,
                    member_count=len(persona.members),
                    platforms=tuple(sorted({m.platform for m in persona.members})),
                    confidence=persona.confidence,
                    resolution_version=persona.resolution_version,
                    status="persona_created",
                    job_run_id=job_run_id,
                )
            evidence_blob = evidence_payload_by_persona[persona.persona_id]
            for member in persona.members:
                out_rows.append(
                    {
                        "persona_id": persona.persona_id,
                        "platform": member.platform,
                        "author_id": member.author_id,
                        "member_count": len(persona.members),
                        "canonical_handle": persona.canonical_handle,
                        "canonical_display_name": persona.canonical_display_name,
                        "confidence": persona.confidence,
                        "resolution_version": persona.resolution_version,
                        "evidence_json": evidence_blob,
                        "activation_date": activation_date_iso,
                        "job_run_id": job_run_id,
                        "run_at": run_at_iso,
                        "pipeline_version": pipeline_version,
                        "schema_version": 1,
                        "source_type": "social",
                        "region": region,
                        "tenant_id": tenant_id,
                    }
                )

    out_fh = out_path.open("w", encoding="utf-8") if out_path else sys.stdout
    try:
        for row in out_rows:
            out_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        if out_path:
            out_fh.close()

    sys.stderr.write(
        f"emitted {len(out_rows)} persona-member rows across "
        f"{len(groups)} regions, version={IDENTITY_RESOLUTION_AUTHOR_VERSION}\n"
    )
    return 0


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Cross-platform author identity resolution (Eixo 3 v2b)"
    )
    parser.add_argument(
        "--authors",
        type=Path,
        required=True,
        help="JSONL with author records (platform, author_id, display_name?, ...)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSONL path; default stdout",
    )
    parser.add_argument(
        "--handle-similarity",
        type=float,
        default=settings.cib.er_handle_similarity,
    )
    parser.add_argument(
        "--display-name-similarity",
        type=float,
        default=settings.cib.er_display_name_similarity,
    )
    parser.add_argument(
        "--min-shared-content",
        type=int,
        default=settings.cib.er_min_shared_content,
    )
    parser.add_argument(
        "--no-content-hash-bridge",
        dest="use_content_hash_bridge",
        action="store_false",
        default=settings.cib.er_use_content_hash_bridge,
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Filter authors to this region slug only.",
    )
    parser.add_argument("--pipeline-version", default="0.1.0")
    parser.add_argument(
        "--no-audit",
        dest="audit_enabled",
        action="store_false",
        default=settings.cib.er_audit_enabled,
    )
    args = parser.parse_args()

    return run(
        authors_path=args.authors,
        out_path=args.out,
        handle_similarity=args.handle_similarity,
        display_name_similarity=args.display_name_similarity,
        min_shared_content=args.min_shared_content,
        use_content_hash_bridge=args.use_content_hash_bridge,
        region_filter=args.region,
        pipeline_version=args.pipeline_version,
        audit_enabled=args.audit_enabled,
    )


if __name__ == "__main__":
    sys.exit(main())
