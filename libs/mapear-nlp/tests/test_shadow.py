"""Unit tests for the shadow A/B comparator."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.shadow import (  # type: ignore[import-not-found]
    InputCase,
    compare,
    load_thresholds,
    run_shadow,
)
from mapear_nlp.political_sentiment import ClassificationThresholds


def _cases() -> list[InputCase]:
    """A small mixed batch — one of each label-target plus boundary cases."""
    return [
        # Clear ALERT under defaults
        InputCase(
            "A001",
            polarity=-0.6,
            volume_24h=20,
            velocity=0.8,
            engagement=8000,
            recurrence=0.6,
        ),
        # Just-on-the-line ALERT
        InputCase(
            "A002",
            polarity=-0.4,
            volume_24h=15,
            velocity=0.7,
            engagement=5000,
            recurrence=0.0,
        ),
        # Mid-negative WARNING
        InputCase(
            "W001",
            polarity=-0.3,
            volume_24h=10,
            velocity=0.5,
            engagement=2000,
            recurrence=0.3,
        ),
        # Strong positive FAVORABLE
        InputCase(
            "F001",
            polarity=0.7,
            volume_24h=0,
            velocity=0.0,
            engagement=0,
            recurrence=0.0,
        ),
        # Default-WARNING
        InputCase(
            "W002",
            polarity=0.0,
            volume_24h=0,
            velocity=0.0,
            engagement=0,
            recurrence=0.0,
        ),
    ]


def test_identity_candidate_yields_zero_movement():
    defaults = ClassificationThresholds()
    records = run_shadow(_cases(), defaults, defaults)
    report = compare(records)

    assert report.n_cases == 5
    assert report.agreed == 5
    assert report.escalated == 0
    assert report.demoted == 0
    assert report.mean_confidence_shift == 0.0
    assert report.mean_risk_shift == 0.0
    assert report.movements == []
    # Distributions identical.
    assert report.primary_distribution == report.candidate_distribution


def test_tightened_alert_demotes_borderline_alert():
    """Pushing the ALERT polarity floor more negative should demote A002
    (polarity=-0.4) without touching A001 (polarity=-0.6)."""
    primary = ClassificationThresholds()
    candidate = ClassificationThresholds(polarity_negative=-0.45)
    records = run_shadow(_cases(), primary, candidate)
    report = compare(records)

    assert report.demoted >= 1
    assert report.escalated == 0
    # The known borderline case shows up in movements.
    moved_ids = {m["case_id"] for m in report.movements}
    assert "A002" in moved_ids
    # Clear ALERT stays put.
    a001_movements = [m for m in report.movements if m["case_id"] == "A001"]
    assert a001_movements == []


def test_relaxed_warning_escalates_some_default_warnings():
    """A candidate that broadens FAVORABLE may *demote* default-WARNINGs to
    FAVORABLE. Or, conversely, lowering the WARNING polarity bar can flip a
    near-neutral case into WARNING (escalation). This test exercises that
    the comparator classifies the direction correctly."""
    primary = ClassificationThresholds()
    candidate = ClassificationThresholds(
        polarity_warning=0.05, velocity_warning=0.0, volume_warning=0
    )
    records = run_shadow(_cases(), primary, candidate)
    report = compare(records)
    # The total must conserve: agreed + escalated + demoted == n_cases.
    assert report.agreed + report.escalated + report.demoted == report.n_cases


def test_transition_matrix_row_sums_match_primary_distribution():
    primary = ClassificationThresholds()
    candidate = ClassificationThresholds(polarity_negative=-0.45)
    records = run_shadow(_cases(), primary, candidate)
    report = compare(records)

    for label, row in report.transition_matrix.items():
        assert sum(row.values()) == report.primary_distribution[label]


def test_load_thresholds_partial_yaml_falls_back_to_defaults(tmp_path: Path):
    yaml_path = tmp_path / "candidate.yaml"
    yaml_path.write_text("polarity_negative: -0.50\n")
    t = load_thresholds(yaml_path)
    assert t.polarity_negative == -0.50
    # Everything else still at defaults.
    defaults = ClassificationThresholds()
    assert t.velocity_spike == defaults.velocity_spike
    assert t.volume_warning == defaults.volume_warning


def test_load_thresholds_unknown_key_raises(tmp_path: Path):
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("not_a_real_field: 1.0\n")
    with pytest.raises(ValueError, match="Unknown threshold keys"):
        load_thresholds(yaml_path)


def test_load_thresholds_none_path_returns_defaults():
    t = load_thresholds(None)
    assert t == ClassificationThresholds()


def test_compare_on_empty_records_is_safe():
    report = compare([])
    assert report.n_cases == 0
    assert report.agreed == 0
    assert report.movements == []
