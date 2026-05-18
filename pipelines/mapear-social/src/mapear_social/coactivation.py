"""Fan out silver social rows into ``silver_author_activations`` records.

Eixo 3 v1 — author co-activation foundation. One silver post that
mentions N politicians becomes N activation rows, one per
(author_id, person_target). The activation table is the input to
``mapear_nlp.graph.coactivation`` (CIB detection); we keep the fan-out
inside mapear-social because all the inputs (mentioned_* arrays,
author_handle, batch_id, lineage stamps) already exist on the silver
row dict the pipeline assembles.

The graph engine itself is *not* invoked here — the pipeline only
persists the activations. Co-activation scoring runs out-of-band (dbt
mart `fct_author_coactivation_daily` for production rollups, plus the
eval harness for precision/recall against the labelled gold set).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Mapping mentioned_* field name → target_kind discriminator on the
# activation row. Order matters: when a single name appears in multiple
# lists (e.g. a mayor who is also a candidate), the first kind wins —
# this keeps the kind stable across pipeline runs even as NER coverage
# improves. ``mentioned_persons`` is the catch-all fallback for names
# that did not resolve to any specific office.
_TARGET_KIND_BY_FIELD: tuple[tuple[str, str], ...] = (
    ("mentioned_mayors", "mayor"),
    ("mentioned_governors", "governor"),
    ("mentioned_candidates", "candidate"),
    ("mentioned_politicians", "politician"),
    ("mentioned_parties", "party"),
    ("mentioned_persons", "person"),
)


def _collect_targets(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return ``[(person_target, target_kind), ...]`` for one silver row.

    De-dupes across the mentioned_* fields with first-kind-wins so the
    same person never appears twice in the same post. Empty / falsy
    strings are dropped.
    """
    seen: dict[str, str] = {}
    for field, kind in _TARGET_KIND_BY_FIELD:
        for name in row.get(field) or ():
            if not name:
                continue
            if name in seen:
                continue
            seen[name] = kind
    return [(name, kind) for name, kind in seen.items()]


def build_activation_records(
    silver_rows: Iterable[Mapping[str, Any]],
    *,
    region: str | None,
    pipeline_version: str,
) -> list[dict[str, Any]]:
    """Fan out silver rows into activation records.

    Only rows with at least one mentioned target produce activations.
    ``person_id`` from the upstream resolver is carried through as
    ``target_person_id`` *only when the activation target matches the
    resolved author's own person_id*, which is rare; for v1 we leave it
    None and rely on the raw name string for graph keying. (Resolving
    each mentioned name to a person_id is a v2 deliverable — see ADR.)

    ``tenant_id`` is intentionally not stamped here; the caller stamps
    it on the parquet DataFrame the same way the silver/raw paths do,
    so the lineage rule "tenant_id is the last stamp before the write"
    stays uniform.
    """
    out: list[dict[str, Any]] = []
    for row in silver_rows:
        targets = _collect_targets(row)
        if not targets:
            continue
        author_handle = row.get("author_handle")
        if not author_handle:
            continue
        for name, kind in targets:
            out.append(
                {
                    "author_id": author_handle,
                    "platform": row["platform"],
                    "post_id": row["post_id"],
                    "content_hash": row["content_hash"],
                    "person_target": name,
                    "published_at": row["published_at"],
                    "target_kind": kind,
                    "target_person_id": None,
                    "author_in_scope": (row.get("scope_status") == "IN_SCOPE"),
                    "extracted_at": row["extracted_at"],
                    "batch_id": row["batch_id"],
                    "actor_run_id": row.get("actor_run_id"),
                    "ingestion_run_id": row.get("ingestion_run_id"),
                    "pipeline_version": pipeline_version,
                    "schema_version": 1,
                    "source_type": "social",
                    "region": region,
                    "tenant_id": None,
                }
            )
    return out


__all__ = ["build_activation_records"]
