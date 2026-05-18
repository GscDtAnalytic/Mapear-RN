"""Out-of-band mayor endorsement investigation job — Eixo 2 v2d.

Designed to run as a Cloud Run Job on a periodic cadence (e.g. daily).
For each monitored mayor it reads a bundle of recent articles that
co-mention the mayor with a gubernatorial candidate, asks the LLM
(Sonnet — see ``LLMConfig.endorsement_model``) to investigate whether
there is evidence of political alignment, and emits one JSONL row per
mayor to ``silver_mayor_endorsements``.

Why out-of-band, not inline in the RSS/social pipelines
-------------------------------------------------------
* The verdict reasons over MANY articles per mayor — it is not a
  per-article hot-path step.
* It can be re-run with a new prompt version without reprocessing the
  pipelines. The side-table is keyed on
  (mayor_id, endorsement_prompt_version), so re-runs are additive.

Input JSONL — one row per mayor
-------------------------------
The orchestrator (Cloud Scheduler → Cloud Run Job) builds this from a
BigQuery query over ``mapear_events`` + ``dim_rn_cities_mayors``.
Required fields per row::

    {
      "mayor_id": "mayor_paulinho_freire",
      "mayor_name": "Paulinho Freire",
      "mayor_party": "União Brasil",
      "candidates": ["Fátima Bezerra", "Cadu Xavier", ...],
      "region": "rn",
      "tenant_id": "mapear",
      "articles": [
        {"article_id": "...", "title": "...", "text": "...",
         "published_at": "2026-05-10", "source": "Tribuna do Norte"},
        ...
      ]
    }

Output JSONL
------------
One row per mayor. Downstream loader MERGEs into
``mapear_silver.silver_mayor_endorsements`` keyed on
``(mayor_id, endorsement_prompt_version)``.

Local invocation
----------------

::

    uv run --package mapear-nlp python -m mapear_nlp.run_mayor_endorsement_detection \\
        --mayors /tmp/mayor_bundles.jsonl \\
        --out /tmp/endorsements.jsonl \\
        --region rn
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mapear_infra.config import get_settings

PIPELINE_VERSION = "0.1.0"


def _load_mayor_bundles(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file of mayor evidence bundles."""
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not row.get("mayor_id"):
                continue
            rows.append(row)
    return rows


def run(
    mayors_path: Path,
    out_path: Path,
    *,
    region_filter: str | None,
    pipeline_version: str,
    detector: Any = None,
    endorsement_enabled: bool = True,
    endorsement_model: str | None = None,
) -> int:
    """Investigate endorsements for every mayor bundle in *mayors_path*.

    Returns the number of rows written. Callers may inject a fake
    ``detector`` for testing without a live LLM.
    """
    if not endorsement_enabled:
        sys.stderr.write("MAPEAR_LLM_ENDORSEMENT_ENABLED=false — nothing to do\n")
        return 0

    rows = _load_mayor_bundles(mayors_path)
    if region_filter is not None:
        rows = [r for r in rows if r.get("region") == region_filter]
    if not rows:
        sys.stderr.write("no mayor bundles after filter — nothing to do\n")
        return 0

    from mapear_infra.privacy import RedactionLevel

    from mapear_nlp.llm.client import get_llm_client
    from mapear_nlp.mayor_endorsement_detector import (
        EndorsementArticle,
        MayorEndorsementDetector,
    )
    from mapear_nlp.narrative_cache import NarrativeCache

    settings = get_settings()

    if detector is None:
        # Endorsement investigation runs on a stronger model than the
        # Haiku explainer — copy the LLM config with the model swapped.
        llm_cfg = settings.llm.model_copy(
            update={"model": endorsement_model or settings.llm.endorsement_model}
        )
        llm_client = get_llm_client(llm_cfg)

        cache = None
        if settings.llm.cache_enabled and settings.gcp.gcs_bucket_name:
            cache = NarrativeCache.build(
                bucket_name=settings.gcp.gcs_bucket_name,
                project_id=settings.gcp.project_id,
                prefix=settings.llm.endorsement_cache_gcs_prefix,
            )

        try:
            pii_level = RedactionLevel(settings.llm.pii_level)
        except ValueError:
            pii_level = RedactionLevel.MASKED
        hmac_key = (
            settings.llm.pii_hmac_key.encode() if settings.llm.pii_hmac_key else None
        )

        detector = MayorEndorsementDetector(
            llm_client,
            cache,
            max_tokens=settings.llm.endorsement_max_tokens,
            timeout_seconds=settings.llm.timeout_seconds,
            redaction_level=pii_level,
            hmac_key=hmac_key,
        )

    model_name = getattr(getattr(detector, "_llm", None), "model", endorsement_model)
    job_run_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    written = 0

    with out_path.open("w", encoding="utf-8") as out_fh:
        for row in rows:
            articles = [
                EndorsementArticle(
                    article_id=str(a.get("article_id") or ""),
                    title=a.get("title") or "",
                    text=a.get("text") or "",
                    published_at=str(a.get("published_at") or ""),
                    source=a.get("source") or "",
                )
                for a in (row.get("articles") or [])
                if a.get("article_id")
            ]
            result = detector.investigate(
                mayor_id=row["mayor_id"],
                mayor_name=row.get("mayor_name") or "",
                mayor_party=row.get("mayor_party") or "",
                candidates=list(row.get("candidates") or []),
                articles=articles,
            )
            out_fh.write(
                json.dumps(
                    {
                        "mayor_id": row["mayor_id"],
                        "mayor_name": row.get("mayor_name"),
                        "mayor_party": row.get("mayor_party"),
                        "endorsement_prompt_version": result.prompt_version,
                        "detected_candidate": result.detected_candidate,
                        "confidence": result.confidence,
                        "rationale": result.rationale,
                        "evidence_ids": result.evidence_ids,
                        "endorsement_model": model_name,
                        "article_count": result.article_count,
                        "cache_hit": result.cache_hit,
                        "error": result.error,
                        "redaction_level": result.redaction_level,
                        "investigated_at": now.isoformat(),
                        "job_run_id": job_run_id,
                        "pipeline_version": pipeline_version,
                        "schema_version": 1,
                        "region": row.get("region"),
                        "tenant_id": row.get("tenant_id"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1

    sys.stderr.write(f"endorsement job done: {written} rows written to {out_path}\n")
    return written


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Investigate mayor endorsements (LLM) — Eixo 2 v2d"
    )
    p.add_argument(
        "--mayors", required=True, type=Path, help="JSONL of mayor evidence bundles"
    )
    p.add_argument("--out", required=True, type=Path, help="Output JSONL path")
    p.add_argument("--region", default=None, help="Filter to this region slug")
    p.add_argument("--pipeline-version", default=PIPELINE_VERSION)
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    settings = get_settings()
    n = run(
        mayors_path=args.mayors,
        out_path=args.out,
        region_filter=args.region,
        pipeline_version=args.pipeline_version,
        endorsement_enabled=settings.llm.endorsement_enabled,
    )
    sys.exit(0 if n >= 0 else 1)
