"""Tests for feed health monitoring."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from mapear_rss.monitoring.feed_health import (
    AVAILABILITY_TIMEOUT_S,
    CONSECUTIVE_FAILURE_ALERT_THRESHOLD,
    FeedHealthMonitor,
)


@pytest.fixture
def monitor() -> FeedHealthMonitor:
    return FeedHealthMonitor(engine=None)


def _ok_response(status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


class TestCheckFeed:
    def test_available_feed_200(self, monitor: FeedHealthMonitor) -> None:
        with patch("httpx.head", return_value=_ok_response(200)):
            health = monitor.check_feed(
                "Tribuna", "https://tribunadonorte.com.br/feed/"
            )

        assert health.is_available is True
        assert health.http_status == 200
        assert health.error_message is None
        assert health.name == "Tribuna"

    def test_http_error_marks_unavailable(self, monitor: FeedHealthMonitor) -> None:
        with patch("httpx.head", return_value=_ok_response(503)):
            health = monitor.check_feed("Dead Feed", "https://dead.com/feed/")

        assert health.is_available is False
        assert health.http_status == 503
        assert "503" in (health.error_message or "")

    def test_http_404_marks_unavailable(self, monitor: FeedHealthMonitor) -> None:
        with patch("httpx.head", return_value=_ok_response(404)):
            health = monitor.check_feed("Gone Feed", "https://gone.com/feed/")

        assert health.is_available is False

    def test_timeout_marks_unavailable(self, monitor: FeedHealthMonitor) -> None:
        with patch("httpx.head", side_effect=httpx.TimeoutException("timed out")):
            health = monitor.check_feed("Slow Feed", "https://slow.com/feed/")

        assert health.is_available is False
        assert health.error_message == "timeout"
        assert health.response_time_ms is None

    def test_connection_error_marks_unavailable(
        self, monitor: FeedHealthMonitor
    ) -> None:
        with patch("httpx.head", side_effect=httpx.ConnectError("refused")):
            health = monitor.check_feed("Offline", "https://offline.com/feed/")

        assert health.is_available is False
        assert health.error_message is not None

    def test_response_time_recorded_on_success(
        self, monitor: FeedHealthMonitor
    ) -> None:
        with patch("httpx.head", return_value=_ok_response(200)):
            health = monitor.check_feed("Fast Feed", "https://fast.com/feed/")

        # response_time_ms is measured in wall-clock time; just ensure non-negative
        assert health.response_time_ms is not None
        assert health.response_time_ms >= 0


class TestCheckAll:
    def test_all_available(self, monitor: FeedHealthMonitor) -> None:
        feeds = [
            ("Feed A", "https://a.com/feed/"),
            ("Feed B", "https://b.com/feed/"),
        ]
        with patch("httpx.head", return_value=_ok_response(200)):
            report = monitor.check_all(feeds)

        assert report.total_feeds == 2
        assert report.available_feeds == 2
        assert report.unavailable_feeds == 0
        assert report.unhealthy == []

    def test_mixed_availability(self, monitor: FeedHealthMonitor) -> None:
        feeds = [
            ("Good Feed", "https://good.com/feed/"),
            ("Bad Feed", "https://bad.com/feed/"),
        ]
        responses = [_ok_response(200), _ok_response(503)]
        with patch("httpx.head", side_effect=responses):
            report = monitor.check_all(feeds)

        assert report.available_feeds == 1
        assert report.unavailable_feeds == 1

    def test_unhealthy_feeds_appear_after_threshold(
        self, monitor: FeedHealthMonitor
    ) -> None:
        feeds = [("Flaky Feed", "https://flaky.com/feed/")]
        with (
            patch("httpx.head", side_effect=httpx.TimeoutException("t")),
            patch.object(
                monitor,
                "_load_consecutive_failures_from_db",
                return_value={
                    "https://flaky.com/feed/": CONSECUTIVE_FAILURE_ALERT_THRESHOLD - 1
                },
            ),
        ):
            report = monitor.check_all(feeds)

        # prior failures (threshold-1) + 1 this run == threshold → unhealthy
        assert "Flaky Feed" in report.unhealthy

    def test_healthy_feed_not_flagged_as_unhealthy(
        self, monitor: FeedHealthMonitor
    ) -> None:
        feeds = [("Healthy", "https://healthy.com/feed/")]
        with patch("httpx.head", return_value=_ok_response(200)):
            report = monitor.check_all(feeds)

        assert "Healthy" not in report.unhealthy

    def test_avg_response_time_computed(self, monitor: FeedHealthMonitor) -> None:
        feeds = [
            ("A", "https://a.com/feed/"),
            ("B", "https://b.com/feed/"),
        ]
        with patch("httpx.head", return_value=_ok_response(200)):
            report = monitor.check_all(feeds)

        assert report.avg_response_time_ms is not None
        assert report.avg_response_time_ms >= 0

    def test_avg_response_time_none_when_all_unavailable(
        self, monitor: FeedHealthMonitor
    ) -> None:
        feeds = [("Down", "https://down.com/feed/")]
        with patch("httpx.head", side_effect=httpx.TimeoutException("t")):
            report = monitor.check_all(feeds)

        assert report.avg_response_time_ms is None


class TestBuildReport:
    def test_build_report_json_serializable(self, monitor: FeedHealthMonitor) -> None:
        feeds = [("Feed A", "https://a.com/feed/")]
        with patch("httpx.head", return_value=_ok_response(200)):
            health_report = monitor.check_all(feeds)

        d = monitor.build_report(health_report, {"https://a.com/feed/": 42})
        json.dumps(d)  # must not raise

    def test_build_report_includes_daily_volume(
        self, monitor: FeedHealthMonitor
    ) -> None:
        feeds = [("Feed A", "https://a.com/feed/")]
        with patch("httpx.head", return_value=_ok_response(200)):
            health_report = monitor.check_all(feeds)

        d = monitor.build_report(health_report, {"https://a.com/feed/": 15})
        assert d["per_feed"][0]["daily_volume"] == 15

    def test_build_report_has_required_keys(self, monitor: FeedHealthMonitor) -> None:
        feeds = [("Feed A", "https://a.com/feed/")]
        with patch("httpx.head", return_value=_ok_response(200)):
            health_report = monitor.check_all(feeds)

        d = monitor.build_report(health_report, {})
        for key in (
            "total_feeds",
            "available_feeds",
            "unavailable_feeds",
            "avg_response_time_ms",
            "unhealthy_feeds",
            "per_feed",
        ):
            assert key in d, f"Missing key: {key}"


class TestConstants:
    def test_default_threshold(self) -> None:
        assert CONSECUTIVE_FAILURE_ALERT_THRESHOLD == 3

    def test_default_timeout(self) -> None:
        assert AVAILABILITY_TIMEOUT_S == 10.0


class TestFeedFallback:
    """Feed failure must not abort the run — other feeds continue."""

    def test_one_feed_down_rest_continue(self) -> None:
        """fetch_multiple continues even when one feed raises an exception."""
        from mapear_rss.discovery.rss_reader import RSSReader

        reader = RSSReader()
        calls: list[str] = []

        def fake_fetch_feed(url: str, **kwargs):  # noqa: ANN001
            calls.append(url)
            if "down" in url:
                raise ConnectionError("feed is down")
            return []

        with patch.object(reader, "fetch_feed", side_effect=fake_fetch_feed):
            result = reader.fetch_multiple(
                [
                    "https://up.com/feed/",
                    "https://down.com/feed/",
                    "https://also-up.com/feed/",
                ],
                min_published_at=None,
            )

        # All three URLs were attempted
        assert len(calls) == 3
        # No exception raised; result is empty list (fake returns [])
        assert result == []

    def test_all_feeds_down_returns_empty(self) -> None:
        from mapear_rss.discovery.rss_reader import RSSReader

        reader = RSSReader()

        with patch.object(reader, "fetch_feed", side_effect=ConnectionError("down")):
            result = reader.fetch_multiple(
                ["https://a.com/feed/", "https://b.com/feed/"],
                min_published_at=None,
            )

        assert result == []
