"""Out-of-band stance-classification job — Eixo 2 v2b.

Designed to run as a Cloud Run Job on a daily cadence. Reads a JSONL
batch of ``GoldArticle`` rows where ``narrative_summary IS NOT NULL``,
classifies the stance of each narrative toward its target official
(favor / contra / neutro), and emits one JSONL stream to
``silver_article_stances``.

Why out-of-band, not inline in the RSS pipeline
-----------------------------------------------
* The stance LLM call is independent of the hot-path classification
  (Stage 4.6 narrative explainer). Adding it inline would couple two
  LLM calls to the same pipeline run.
* Stance can be re-run with a new prompt version (stance_v2, v3) without
  reprocessing the entire pipeline. The side-table design (keyed on
  (content_hash, stance_prompt_version)) makes this additive.
* Stance is downstream of narrative_summary, which is already gated on
  sentiment_label=ALERT. Running stance inline would add no extra rows
  but would require the hot path to carry the classifier.

Inputs
------
JSONL of GoldArticle rows. The orchestrator (Cloud Scheduler →
Cloud Run Job) is expected to ``bq extract`` ``mapear_gold.gold_articles``
into GCS just before invoking. Required fields per row:
``content_hash``, ``narrative_summary``, ``person_id``, ``person_name``
(or carry fields), ``person_role``, ``rule_version``, ``region``,
``tenant_id``.

Outputs
-------
One JSONL file. Downstream loader MERGEs into
``mapear_silver.silver_article_stances`` keyed on
``(content_hash, stance_prompt_version)``.

Local invocation
----------------

::

    poetry run python -m mapear_nlp.run_stance_classification \\
        --gold /tmp/gold_articles.jsonl \\
        --out /tmp/stances.jsonl \\
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


def _load_gold(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file of GoldArticle rows.

    Filters to rows with a non-empty ``narrative_summary``. Rows without
    a summary (pre-Eixo-2-v1, WARNING/FAVORABLE) are silently skipped —
    they have nothing to classify.
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
            rows.append(row)
    return rows


def run(
    gold_path: Path,
    out_path: Path,
    *,
    region_filter: str | None,
    pipeline_version: str,
    llm_client: Any = None,
    cache: Any = None,
    stance_enabled: bool = True,
) -> int:
    """Classify stance for all narratives in *gold_path*.

    Returns the number of rows written. Callers may inject a fake
    ``llm_client`` and ``cache`` for testing without a live LLM.
    """
    if not stance_enabled:
        sys.stderr.write("MAPEAR_LLM_STANCE_ENABLED=false — nothing to do\n")
        return 0

    rows = _load_gold(gold_path)
    if region_filter is not None:
        rows = [r for r in rows if r.get("region") == region_filter]
    if not rows:
        sys.stderr.write("no narratives after filter — nothing to do\n")
        return 0

    from mapear_infra.privacy import RedactionLevel

    from mapear_nlp.llm.client import get_llm_client
    from mapear_nlp.narrative_cache import NarrativeCache
    from mapear_nlp.stance_classifier import StanceClassifier

    settings = get_settings()

    if llm_client is None:
        llm_client = get_llm_client(settings.llm)

    if cache is None and settings.llm.cache_enabled and settings.gcp.gcs_bucket_name:
        cache = NarrativeCache.build(
            bucket_name=settings.gcp.gcs_bucket_name,
            project_id=settings.gcp.project_id,
            prefix=settings.llm.stance_cache_gcs_prefix,
        )

    try:
        pii_level = RedactionLevel(settings.llm.pii_level)
    except ValueError:
        pii_level = RedactionLevel.MASKED

    hmac_key = settings.llm.pii_hmac_key.encode() if settings.llm.pii_hmac_key else None

    classifier = StanceClassifier(
        llm_client,
        cache,
        max_tokens=60,
        temperature=0.1,
        timeout_seconds=settings.llm.timeout_seconds,
        redaction_level=pii_level,
        hmac_key=hmac_key,
    )

    job_run_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    written = 0

    with out_path.open("w", encoding="utf-8") as out_fh:
        for row in rows:
            result = classifier.classify(
                content_hash=row["content_hash"],
                narrative_summary=row["narrative_summary"],
                person_name=row.get("person_name") or "",
                person_role=row.get("role") or "",
                rule_version=row.get("rule_version") or "",
            )
            stance_row = {
                "content_hash": row["content_hash"],
                "stance_prompt_version": result.prompt_version,
                "stance_label": result.stance_label,
                "confidence": result.confidence,
                "stance_model": llm_client.model,
                "cache_hit": result.cache_hit,
                "error": result.error,
                "redaction_level": result.redaction_level,
                "person_id": row.get("person_id"),
                "person_name": row.get("person_name"),
                "person_role": row.get("role"),
                "classified_at": now.isoformat(),
                "job_run_id": job_run_id,
                "pipeline_version": pipeline_version,
                "schema_version": 1,
                "source_type": row.get("source_type", "rss"),
                "region": row.get("region"),
                "tenant_id": row.get("tenant_id"),
            }
            out_fh.write(json.dumps(stance_row, ensure_ascii=False) + "\n")
            written += 1

    sys.stderr.write(f"stance job done: {written} rows written to {out_path}\n")
    return written


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Classify narrative stance (favor/contra/neutro) — Eixo 2 v2b"
    )
    p.add_argument("--gold", required=True, type=Path, help="JSONL of GoldArticle rows")
    p.add_argument("--out", required=True, type=Path, help="Output JSONL path")
    p.add_argument("--region", default=None, help="Filter to this region slug")
    p.add_argument("--pipeline-version", default=PIPELINE_VERSION)
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    settings = get_settings()
    n = run(
        gold_path=args.gold,
        out_path=args.out,
        region_filter=args.region,
        pipeline_version=args.pipeline_version,
        stance_enabled=settings.llm.stance_enabled,
    )
    sys.exit(0 if n >= 0 else 1)
