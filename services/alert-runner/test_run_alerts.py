"""Unit tests for the alert runner.

google-cloud-bigquery is only installed in the runner image, so we
``importorskip`` it — dev environments without the SDK still pass.
"""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

pytest.importorskip("google.cloud.bigquery")

sys.path.insert(0, os.path.dirname(__file__))

import run_alerts  # noqa: E402

_SPIKE_ROW = {
    "person_name": "Fátima Bezerra",
    "person_role": "governor",
    "mentions": 47,
    "zscore": 3.10,
}
_CIB_ROW = {
    "community_id": "abc123",
    "community_size": 8,
    "composite_score": 0.82,
    "avg_synchrony_score": 0.91,
    "avg_alignment_score": 0.74,
    "series_age_days": 5,
}


def _mock_bq(spike_rows=None, cib_rows=None):
    """Return a BQ client mock whose .query().result() cycles spike → cib."""
    client = mock.MagicMock()
    result_mock = mock.MagicMock()
    result_mock.result.side_effect = [
        spike_rows if spike_rows is not None else [],
        cib_rows if cib_rows is not None else [],
    ]
    client.query.return_value = result_mock
    return client


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------


def test_disabled_skips_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "false")
    client = mock.MagicMock()
    assert run_alerts.run(bq_client=client) == 0
    client.query.assert_not_called()


# ---------------------------------------------------------------------------
# No anomalies / no clusters → no notifications
# ---------------------------------------------------------------------------


def test_no_anomalies_no_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    spike_mock = mock.MagicMock(return_value=True)
    cib_mock = mock.MagicMock(return_value=True)

    result = run_alerts.run(
        bq_client=_mock_bq(),
        spike_notifier=spike_mock,
        cib_notifier=cib_mock,
    )

    assert result == 0
    spike_mock.assert_not_called()
    cib_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Spike alert
# ---------------------------------------------------------------------------


def test_spike_alert_fires_when_anomaly_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("MAPEAR_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    spike_mock = mock.MagicMock(return_value=True)
    cib_mock = mock.MagicMock(return_value=True)

    result = run_alerts.run(
        bq_client=_mock_bq(spike_rows=[_SPIKE_ROW]),
        spike_notifier=spike_mock,
        cib_notifier=cib_mock,
    )

    assert result == 0
    spike_mock.assert_called_once()
    kwargs = spike_mock.call_args.kwargs
    assert kwargs["spikes"] == [_SPIKE_ROW]
    assert kwargs["run_date"] is not None
    cib_mock.assert_not_called()


def test_spike_notifier_receives_correct_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("MAPEAR_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/abc")
    spike_mock = mock.MagicMock(return_value=True)

    run_alerts.run(
        bq_client=_mock_bq(spike_rows=[_SPIKE_ROW]),
        spike_notifier=spike_mock,
    )

    assert spike_mock.call_args.kwargs["webhook_url"] == "https://hooks.slack.com/abc"


# ---------------------------------------------------------------------------
# CIB cluster alert
# ---------------------------------------------------------------------------


def test_cib_alert_fires_when_cluster_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("MAPEAR_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    spike_mock = mock.MagicMock(return_value=True)
    cib_mock = mock.MagicMock(return_value=True)

    result = run_alerts.run(
        bq_client=_mock_bq(cib_rows=[_CIB_ROW]),
        spike_notifier=spike_mock,
        cib_notifier=cib_mock,
    )

    assert result == 0
    spike_mock.assert_not_called()
    cib_mock.assert_called_once()
    kwargs = cib_mock.call_args.kwargs
    assert kwargs["clusters"] == [_CIB_ROW]


def test_cib_query_passes_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("MAPEAR_ALERT_CIB_COMPOSITE_SCORE_THRESHOLD", "0.85")
    monkeypatch.setenv("MAPEAR_ALERT_CIB_SERIES_AGE_DAYS", "5")

    client = mock.MagicMock()
    result_mock = mock.MagicMock()
    result_mock.result.side_effect = [[], []]
    client.query.return_value = result_mock

    run_alerts.run(bq_client=client)

    # Second call is the CIB query; check query_parameters carry overrides.
    cib_call_config = client.query.call_args_list[1][1]["job_config"]
    params = {p.name: p.value for p in cib_call_config.query_parameters}
    assert params["composite_threshold"] == pytest.approx(0.85)
    assert params["series_age_days"] == 5


# ---------------------------------------------------------------------------
# Both alerts
# ---------------------------------------------------------------------------


def test_both_alerts_fire_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("MAPEAR_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    spike_mock = mock.MagicMock(return_value=True)
    cib_mock = mock.MagicMock(return_value=True)

    result = run_alerts.run(
        bq_client=_mock_bq(spike_rows=[_SPIKE_ROW], cib_rows=[_CIB_ROW]),
        spike_notifier=spike_mock,
        cib_notifier=cib_mock,
    )

    assert result == 0
    spike_mock.assert_called_once()
    cib_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


def test_spike_bq_error_does_not_suppress_cib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    cib_mock = mock.MagicMock(return_value=True)

    client = mock.MagicMock()
    result_mock = mock.MagicMock()
    result_mock.result.side_effect = [RuntimeError("BQ auth error"), [_CIB_ROW]]
    client.query.return_value = result_mock

    result = run_alerts.run(bq_client=client, cib_notifier=cib_mock)

    assert result == 0
    cib_mock.assert_called_once()


def test_notifier_failure_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    spike_mock = mock.MagicMock(side_effect=RuntimeError("Slack down"))

    result = run_alerts.run(
        bq_client=_mock_bq(spike_rows=[_SPIKE_ROW]),
        spike_notifier=spike_mock,
    )

    assert result == 0


def test_no_webhook_skips_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_ALERT_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.delenv("MAPEAR_ALERT_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    spike_mock = mock.MagicMock(return_value=False)

    result = run_alerts.run(
        bq_client=_mock_bq(spike_rows=[_SPIKE_ROW]),
        spike_notifier=spike_mock,
    )

    assert result == 0
