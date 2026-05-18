"""Tests for the shadow scorer (Stage 1E v2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mapear_nlp.political_sentiment import (
    ClassificationThresholds,
    PoliticalSentimentClassifier,
)
from mapear_nlp.shadow.scorer import (
    ShadowScorer,
    build_shadow_scorer,
    load_shadow_thresholds,
)


def _classify(thresholds: ClassificationThresholds, **kw):
    return PoliticalSentimentClassifier(thresholds).classify(**kw)


def test_load_partial_yaml_keeps_other_defaults(tmp_path: Path) -> None:
    p = tmp_path / "candidate.yaml"
    p.write_text("polarity_negative: -0.50\nvelocity_spike: 0.85\n")

    candidate = load_shadow_thresholds(p)

    defaults = ClassificationThresholds()
    assert candidate.polarity_negative == pytest.approx(-0.50)
    assert candidate.velocity_spike == pytest.approx(0.85)
    assert candidate.volume_spike == defaults.volume_spike
    assert candidate.polarity_warning == defaults.polarity_warning


def test_load_unknown_key_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("not_a_threshold: 1.0\n")

    with pytest.raises(ValueError, match="Unknown threshold keys"):
        load_shadow_thresholds(p)


def test_load_empty_path_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        load_shadow_thresholds("")


def test_score_emits_shadow_row_with_primary_snapshot(tmp_path: Path) -> None:
    primary_thresholds = ClassificationThresholds()
    candidate_yaml = tmp_path / "tightened.yaml"
    candidate_yaml.write_text("polarity_negative: -0.50\n")

    primary_result = _classify(
        primary_thresholds,
        polarity=-0.40,
        volume_24h=20,
        velocity=0.8,
        engagement=8000,
    )
    scorer = ShadowScorer(
        candidate=load_shadow_thresholds(candidate_yaml),
        region="rn",
        tenant_id="default",
        pipeline_version="0.42.0",
        source_type="rss",
    )

    row = scorer.score(
        content_hash="abc123",
        polarity=-0.40,
        volume_24h=20,
        velocity=0.8,
        engagement=8000,
        primary=primary_result,
        person_id="mayor_natal",
    )

    assert row.content_hash == "abc123"
    assert row.primary_label == "ALERT"
    # Tightening polarity_negative to -0.50 demotes the -0.40 case below ALERT.
    assert row.shadow_label != "ALERT"
    assert row.primary_rule_version == primary_result.rule_version
    assert row.shadow_rule_version != row.primary_rule_version
    assert row.region == "rn"
    assert row.tenant_id == "default"
    assert row.pipeline_version == "0.42.0"
    assert row.source_type == "rss"
    assert row.shadow_decision_factors  # at least one factor
    assert row.processed_at_utc is not None


def test_build_shadow_scorer_returns_none_when_yaml_empty() -> None:
    assert (
        build_shadow_scorer(
            yaml_path="",
            enabled=True,
            region="rn",
            tenant_id=None,
            pipeline_version=None,
            source_type="rss",
        )
        is None
    )


def test_build_shadow_scorer_returns_none_when_disabled(tmp_path: Path) -> None:
    p = tmp_path / "candidate.yaml"
    p.write_text("polarity_negative: -0.40\n")

    assert (
        build_shadow_scorer(
            yaml_path=str(p),
            enabled=False,
            region="rn",
            tenant_id=None,
            pipeline_version=None,
            source_type="rss",
        )
        is None
    )


def test_build_shadow_scorer_yaml_error_propagates(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("not_a_threshold: 1.0\n")

    with pytest.raises(ValueError, match="Unknown threshold keys"):
        build_shadow_scorer(
            yaml_path=str(bad),
            enabled=True,
            region="rn",
            tenant_id=None,
            pipeline_version=None,
            source_type="rss",
        )


def test_score_all_bulk_helper(tmp_path: Path) -> None:
    p = tmp_path / "candidate.yaml"
    p.write_text("polarity_negative: -0.30\n")
    candidate = load_shadow_thresholds(p)
    scorer = ShadowScorer(
        candidate=candidate,
        region="rn",
        tenant_id=None,
        pipeline_version="0.42.0",
        source_type="social",
    )
    primary = _classify(
        ClassificationThresholds(),
        polarity=-0.20,
        volume_24h=10,
        velocity=0.5,
        engagement=2000,
    )

    rows = scorer.score_all(
        [
            {
                "content_hash": f"h{i}",
                "polarity": -0.20,
                "volume_24h": 10,
                "velocity": 0.5,
                "engagement": 2000,
                "primary": primary,
                "person_id": None,
            }
            for i in range(3)
        ]
    )
    assert [r.content_hash for r in rows] == ["h0", "h1", "h2"]
    assert all(r.shadow_rule_version == candidate.rule_version() for r in rows)


def test_yaml_string_path_accepted(tmp_path: Path) -> None:
    p = tmp_path / "candidate.yaml"
    p.write_text("polarity_negative: -0.45\n")

    candidate = load_shadow_thresholds(str(p))
    assert candidate.polarity_negative == pytest.approx(-0.45)
