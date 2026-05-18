"""Tests for send_spike_alert() and send_cib_alert() in mapear_infra.notifier."""

from __future__ import annotations

from unittest import mock

import pytest

from mapear_infra.notifier import send_cib_alert, send_spike_alert

_SPIKE = [
    {"person_name": "Fátima", "person_role": "governor", "mentions": 47, "zscore": 3.1},
    {
        "person_name": "Carlos",
        "person_role": "candidate",
        "mentions": 28,
        "zscore": 2.6,
    },
]

_CLUSTERS = [
    {
        "community_id": "abc123",
        "community_size": 8,
        "composite_score": 0.82,
        "avg_synchrony_score": 0.91,
        "avg_alignment_score": 0.74,
        "series_age_days": 5,
    }
]


# ---------------------------------------------------------------------------
# send_spike_alert
# ---------------------------------------------------------------------------


def test_spike_no_webhook_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert send_spike_alert(_SPIKE, "2026-05-14", webhook_url="") is False


def test_spike_sends_to_slack() -> None:
    with mock.patch("mapear_infra.notifier.httpx") as mock_httpx:
        mock_httpx.post.return_value.raise_for_status.return_value = None
        result = send_spike_alert(
            _SPIKE, "2026-05-14", webhook_url="https://hooks.slack.com/x"
        )

    assert result is True
    payload = mock_httpx.post.call_args.kwargs["json"]
    # Header block contains the date
    header_text = payload["blocks"][0]["text"]["text"]
    assert "2026-05-14" in header_text
    # Body block contains person name
    body_text = payload["blocks"][1]["text"]["text"]
    assert "Fátima" in body_text


def test_spike_truncates_to_five_entries() -> None:
    many = [
        {"person_name": f"P{i}", "person_role": "mayor", "mentions": 10, "zscore": 2.1}
        for i in range(8)
    ]
    with mock.patch("mapear_infra.notifier.httpx") as mock_httpx:
        mock_httpx.post.return_value.raise_for_status.return_value = None
        send_spike_alert(many, "2026-05-14", webhook_url="https://hooks.slack.com/x")

    body_text = mock_httpx.post.call_args.kwargs["json"]["blocks"][1]["text"]["text"]
    # "e mais 3 anomalia(s)" suffix present when len > 5
    assert "3" in body_text


def test_spike_httpx_failure_returns_false() -> None:
    with mock.patch("mapear_infra.notifier.httpx") as mock_httpx:
        mock_httpx.post.side_effect = ConnectionError("timeout")
        result = send_spike_alert(
            _SPIKE, "2026-05-14", webhook_url="https://hooks.slack.com/x"
        )

    assert result is False


# ---------------------------------------------------------------------------
# send_cib_alert
# ---------------------------------------------------------------------------


def test_cib_no_webhook_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert send_cib_alert(_CLUSTERS, "2026-05-14", webhook_url="") is False


def test_cib_sends_to_slack() -> None:
    with mock.patch("mapear_infra.notifier.httpx") as mock_httpx:
        mock_httpx.post.return_value.raise_for_status.return_value = None
        result = send_cib_alert(
            _CLUSTERS, "2026-05-14", webhook_url="https://hooks.slack.com/x"
        )

    assert result is True
    payload = mock_httpx.post.call_args.kwargs["json"]
    header_text = payload["blocks"][0]["text"]["text"]
    assert "2026-05-14" in header_text
    body_text = payload["blocks"][1]["text"]["text"]
    assert "abc123" in body_text
    assert "série 5d" in body_text


def test_cib_falls_back_to_env_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/env")
    with mock.patch("mapear_infra.notifier.httpx") as mock_httpx:
        mock_httpx.post.return_value.raise_for_status.return_value = None
        result = send_cib_alert(_CLUSTERS, "2026-05-14", webhook_url="")

    assert result is True
    assert mock_httpx.post.call_args.args[0] == "https://hooks.slack.com/env"


def test_cib_httpx_failure_returns_false() -> None:
    with mock.patch("mapear_infra.notifier.httpx") as mock_httpx:
        mock_httpx.post.side_effect = ConnectionError("timeout")
        result = send_cib_alert(
            _CLUSTERS, "2026-05-14", webhook_url="https://hooks.slack.com/x"
        )

    assert result is False
