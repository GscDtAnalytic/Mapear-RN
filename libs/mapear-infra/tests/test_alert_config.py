"""Tests for AlertConfig (semantic alerting thresholds and channel config)."""

from __future__ import annotations

import pytest

from mapear_infra.config import AlertConfig


def test_defaults() -> None:
    cfg = AlertConfig()
    assert cfg.enabled is True
    assert cfg.spike_zscore_threshold == pytest.approx(2.0)
    assert cfg.cib_composite_score_threshold == pytest.approx(0.7)
    assert cfg.cib_series_age_days == 3
    assert cfg.slack_webhook_url == ""


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "false")
    monkeypatch.setenv("MAPEAR_ALERT_CIB_COMPOSITE_SCORE_THRESHOLD", "0.85")
    monkeypatch.setenv("MAPEAR_ALERT_CIB_SERIES_AGE_DAYS", "5")
    monkeypatch.setenv("MAPEAR_ALERT_SPIKE_ZSCORE_THRESHOLD", "3.0")
    monkeypatch.setenv("MAPEAR_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")

    cfg = AlertConfig()

    assert cfg.enabled is False
    assert cfg.cib_composite_score_threshold == pytest.approx(0.85)
    assert cfg.cib_series_age_days == 5
    assert cfg.spike_zscore_threshold == pytest.approx(3.0)
    assert cfg.slack_webhook_url == "https://hooks.slack.com/test"
