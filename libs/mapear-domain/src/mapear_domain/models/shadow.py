"""Pydantic schema for Stage 1E v2 shadow scoring persistence.

Stage 1E v1 (``mapear-nlp/eval/shadow.py``) gave operators a
Python-only A/B comparator for ``ClassificationThresholds``. v2 closes
the loop by writing every shadow classification to the warehouse so
analysts can compare regimes over weeks of production traffic without
exporting CSVs.

Grain
-----
``(content_hash, shadow_rule_version)`` — one row per (content event,
candidate threshold set). A pipeline run with a single shadow regime
produces one shadow row per gold/silver event; rotating the candidate
YAML adds rows alongside, never overwrites.

Design note: primary outputs are persisted alongside shadow outputs in
the same row. The alternative (join with ``gold_articles`` on
``content_hash`` for the primary side) would force every comparison
query to hit two tables across two datasets — cheap individually,
expensive when an analyst is iterating on a candidate. Co-locating
keeps ``mart_rule_version_compare`` SELECT * style and lets the
warehouse partition prune both regimes together.

The shadow row is written by the live pipeline (RSS Stage 4.5b, social
Stage 5.5b), opt-in via ``MAPEAR_SHADOW_RULE_VERSION_YAML``. Empty
env var = noop; no shadow rows emitted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from mapear_domain.schemas import DecisionFactor

SentimentLabel = Literal["FAVORABLE", "WARNING", "ALERT"]


class SilverEventShadow(BaseModel):
    """One shadow A/B classification per (content_hash, shadow_rule_version).

    Persisted by the dual-classifier path in RSS / social pipelines when
    Stage 1E v2 is enabled. The ``primary_*`` fields snapshot the live
    overlay output for the same event so downstream comparison queries
    never join back to ``gold_articles`` / ``silver_social_posts``.
    """

    # --- Grain ---
    content_hash: str
    shadow_rule_version: str
    primary_rule_version: str

    # --- Classifier inputs (identical for both regimes — kept for trace) ---
    polarity: float
    volume_24h: int
    velocity: float
    engagement: int
    recurrence: float = 0.0

    # --- Primary regime outputs (live thresholds in production) ---
    primary_label: SentimentLabel
    primary_confidence: float
    primary_risk_score: float

    # --- Shadow regime outputs (candidate thresholds under evaluation) ---
    shadow_label: SentimentLabel
    shadow_confidence: float
    shadow_risk_score: float
    shadow_decision_factors: list[DecisionFactor] = Field(default_factory=list)

    # --- Optional cross-table lineage to the originating event ---
    person_id: str | None = None
    source_type: str = "rss"

    # --- Lineage / governance ---
    region: str | None = None
    tenant_id: str | None = None
    pipeline_version: str | None = None
    model_version: str
    actor_run_id: str | None = None
    ingestion_run_id: str | None = None
    processed_at_utc: datetime
    schema_version: int = 1


__all__ = ["SilverEventShadow", "SentimentLabel"]
