"""Shadow scorer — runs candidate thresholds alongside primary classifier.

Single entry point for both pipelines (RSS + social). The scorer takes
the same numeric inputs the primary classifier receives, plus the
already-computed primary :class:`ClassificationResult`, and returns one
:class:`SilverEventShadow` row per content event.

Threshold loading
-----------------
``load_shadow_thresholds(path)`` reads a YAML and returns a fully
populated :class:`ClassificationThresholds`. Identical to the v1 helper
in ``mapear-nlp/eval/shadow.py`` but without the CLI baggage. Partial
YAMLs (only the keys to override) are valid; missing keys fall back to
``ClassificationThresholds()`` defaults so calibration drift is
transparent.

Both local paths and ``gs://`` URIs are supported. ``gs://`` requires
``google-cloud-storage`` (already installed in every pipeline image
that runs the LLM narrative explainer).

Why not reuse v1
----------------
The v1 ``run_shadow`` works on numeric :class:`InputCase` rows from a
CSV. The pipeline already has the primary :class:`ClassificationResult`
in hand and persists much richer lineage (region, tenant_id,
pipeline_version, content_hash). Building a tiny adapter that re-derives
the primary regime would cost an extra classifier call per row and
double the number of rule_version objects in scope. The thin scorer
here keeps the live path branch minimal.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from mapear_domain.models.shadow import SilverEventShadow

from mapear_nlp.political_sentiment import (
    ClassificationResult,
    ClassificationThresholds,
    PoliticalSentimentClassifier,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


def load_shadow_thresholds(path: str | Path) -> ClassificationThresholds:
    """Load candidate thresholds from a local file or ``gs://`` URI.

    Empty / missing path raises ``ValueError`` so the pipeline fails
    loud at startup rather than silently disabling shadow.
    """
    if not path:
        raise ValueError("shadow rule_version_yaml is empty")

    path_str = str(path)
    if path_str.startswith("gs://"):
        text = _read_gcs_text(path_str)
    else:
        text = Path(path_str).read_text()

    data = yaml.safe_load(text) or {}
    defaults = dataclasses.asdict(ClassificationThresholds())
    unknown = set(data) - set(defaults)
    if unknown:
        raise ValueError(
            f"Unknown threshold keys in {path_str}: {sorted(unknown)}. "
            f"Allowed: {sorted(defaults)}"
        )
    defaults.update(data)
    return ClassificationThresholds(**defaults)


def _read_gcs_text(uri: str) -> str:
    from google.cloud import storage  # noqa: PLC0415  - optional GCP dep

    bucket_name, _, blob_path = uri[len("gs://") :].partition("/")
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    return bucket.blob(blob_path).download_as_text()


class ShadowScorer:
    """Stateful wrapper around a candidate :class:`PoliticalSentimentClassifier`.

    One scorer per pipeline run. ``score`` is called per event with the
    same inputs the primary classifier already saw and the primary
    :class:`ClassificationResult`. Output is one :class:`SilverEventShadow`
    row ready for the warehouse loader.

    The scorer never raises on per-event errors at the caller site — a
    candidate threshold set is configured upfront and produces deterministic
    output for any numeric input. If the YAML loader threw at startup, the
    pipeline never reached this point.
    """

    def __init__(
        self,
        candidate: ClassificationThresholds,
        *,
        region: str | None,
        tenant_id: str | None,
        pipeline_version: str | None,
        source_type: str,
    ) -> None:
        self._candidate = candidate
        self._classifier = PoliticalSentimentClassifier(candidate)
        self._region = region
        self._tenant_id = tenant_id
        self._pipeline_version = pipeline_version
        self._source_type = source_type

    @property
    def shadow_rule_version(self) -> str:
        return self._candidate.rule_version()

    def score(
        self,
        *,
        content_hash: str,
        polarity: float,
        volume_24h: int,
        velocity: float,
        engagement: int,
        recurrence: float = 0.0,
        primary: ClassificationResult,
        person_id: str | None,
        actor_run_id: str | None = None,
        ingestion_run_id: str | None = None,
    ) -> SilverEventShadow:
        shadow = self._classifier.classify(
            polarity=polarity,
            volume_24h=volume_24h,
            velocity=velocity,
            engagement=engagement,
            recurrence=recurrence,
        )
        return SilverEventShadow(
            content_hash=content_hash,
            shadow_rule_version=shadow.rule_version,
            primary_rule_version=primary.rule_version,
            polarity=polarity,
            volume_24h=volume_24h,
            velocity=velocity,
            engagement=engagement,
            recurrence=recurrence,
            primary_label=primary.label,
            primary_confidence=primary.confidence,
            primary_risk_score=primary.risk_score,
            shadow_label=shadow.label,
            shadow_confidence=shadow.confidence,
            shadow_risk_score=shadow.risk_score,
            shadow_decision_factors=shadow.factors_as_dicts(),
            person_id=person_id,
            source_type=self._source_type,
            region=self._region,
            tenant_id=self._tenant_id,
            pipeline_version=self._pipeline_version,
            model_version=shadow.model_version,
            actor_run_id=actor_run_id,
            ingestion_run_id=ingestion_run_id,
            processed_at_utc=datetime.now(UTC),
        )

    def score_all(self, events: Iterable[dict]) -> list[SilverEventShadow]:
        """Bulk variant — convenience for callers with prebuilt event dicts.

        Each ``event`` must carry the same keys :meth:`score` accepts.
        Used by tests and by the social pipeline loop.
        """
        return [self.score(**e) for e in events]


def build_shadow_scorer(
    *,
    yaml_path: str,
    enabled: bool,
    region: str | None,
    tenant_id: str | None,
    pipeline_version: str | None,
    source_type: str,
) -> ShadowScorer | None:
    """Factory used by pipelines.

    Returns None when shadow is disabled (empty YAML path or
    ``enabled=False``); callers branch on ``is None`` and skip the
    second-classifier call entirely. Failures loading the YAML raise so
    a misconfigured deployment fails fast in the entrypoint, not on
    the first event.
    """
    if not yaml_path or not enabled:
        return None
    candidate = load_shadow_thresholds(yaml_path)
    return ShadowScorer(
        candidate=candidate,
        region=region,
        tenant_id=tenant_id,
        pipeline_version=pipeline_version,
        source_type=source_type,
    )
