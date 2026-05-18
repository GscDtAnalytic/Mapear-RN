"""Feed health monitoring: availability, error rate, and daily volume.

Runs lightweight HTTP HEAD checks before discovery to detect dead feeds
early, and tracks per-feed metrics so the run_report can surface them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from loguru import logger
from sqlalchemy import text
from sqlalchemy.engine import Engine

AVAILABILITY_TIMEOUT_S: float = 10.0
CONSECUTIVE_FAILURE_ALERT_THRESHOLD: int = 3


@dataclass
class FeedHealth:
    """Health snapshot for a single feed."""

    name: str
    url: str
    is_available: bool
    response_time_ms: int | None
    http_status: int | None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    consecutive_failures: int = 0
    error_message: str | None = None


@dataclass
class FeedHealthReport:
    """Aggregated health metrics for all feeds in a run."""

    total_feeds: int
    available_feeds: int
    unavailable_feeds: int
    feeds: list[FeedHealth]
    avg_response_time_ms: float | None
    # Feed names whose consecutive_failures >= threshold
    unhealthy: list[str]


class FeedHealthMonitor:
    """Checks feed availability and tracks per-feed error rates."""

    def __init__(
        self,
        engine: Engine | None = None,
        consecutive_failure_threshold: int = CONSECUTIVE_FAILURE_ALERT_THRESHOLD,
        timeout_s: float = AVAILABILITY_TIMEOUT_S,
    ) -> None:
        self._engine = engine
        self._threshold = consecutive_failure_threshold
        self._timeout_s = timeout_s

    def check_feed(self, name: str, url: str) -> FeedHealth:
        """Perform an HTTP HEAD check for a single feed URL."""
        start = time.perf_counter()
        try:
            resp = httpx.head(
                url,
                timeout=self._timeout_s,
                follow_redirects=True,
                headers={"User-Agent": "Mapear-RN/1.0 FeedHealthChecker"},
            )
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            is_ok = resp.status_code < 400
            health = FeedHealth(
                name=name,
                url=url,
                is_available=is_ok,
                response_time_ms=elapsed_ms,
                http_status=resp.status_code,
                error_message=f"HTTP {resp.status_code}" if not is_ok else None,
            )
        except httpx.TimeoutException:
            health = FeedHealth(
                name=name,
                url=url,
                is_available=False,
                response_time_ms=None,
                http_status=None,
                error_message="timeout",
            )
        except Exception as exc:
            health = FeedHealth(
                name=name,
                url=url,
                is_available=False,
                response_time_ms=None,
                http_status=None,
                error_message=str(exc)[:200],
            )

        if health.is_available:
            logger.debug(
                "Feed available: {name} ({ms}ms)",
                name=name,
                ms=health.response_time_ms,
            )
        else:
            logger.warning(
                "Feed unavailable: {name} — {err}",
                name=name,
                err=health.error_message,
            )
        return health

    def check_all(self, feeds: list[tuple[str, str]]) -> FeedHealthReport:
        """Check availability for all feeds.

        Args:
            feeds: List of (name, url) tuples.

        Returns:
            FeedHealthReport with per-feed health and aggregate metrics.
        """
        past_failures = self._load_consecutive_failures_from_db()
        results: list[FeedHealth] = []

        for name, url in feeds:
            health = self.check_feed(name, url)
            prior = past_failures.get(url, 0)
            health.consecutive_failures = prior + (0 if health.is_available else 1)
            results.append(health)

        available = [h for h in results if h.is_available]
        times = [
            h.response_time_ms for h in available if h.response_time_ms is not None
        ]
        avg_ms = round(sum(times) / len(times), 1) if times else None

        unhealthy = [
            h.name for h in results if h.consecutive_failures >= self._threshold
        ]

        report = FeedHealthReport(
            total_feeds=len(results),
            available_feeds=len(available),
            unavailable_feeds=len(results) - len(available),
            feeds=results,
            avg_response_time_ms=avg_ms,
            unhealthy=unhealthy,
        )

        if report.unavailable_feeds > 0:
            logger.warning(
                "Feed health check: {av}/{total} available, down={names}",
                av=report.available_feeds,
                total=report.total_feeds,
                names=[h.name for h in results if not h.is_available],
            )

        if unhealthy:
            logger.error(
                "Feeds with ≥{n} consecutive failures: {names} — "
                "investigate or deactivate.",
                n=self._threshold,
                names=unhealthy,
            )

        self._persist_health(results)
        return report

    def get_daily_volumes(self, date: datetime | None = None) -> dict[str, int]:
        """Return completed article count per source_feed for a given date."""
        if self._engine is None:
            return {}
        target_date = (date or datetime.now(UTC)).date()
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT source_feed, COUNT(*) AS cnt
                        FROM url_frontier
                        WHERE DATE(discovered_at AT TIME ZONE 'UTC') = :dt
                          AND status = 'completed'
                        GROUP BY source_feed
                    """
                    ),
                    {"dt": str(target_date)},
                ).fetchall()
            return {row.source_feed: row.cnt for row in rows}
        except Exception as exc:
            logger.debug("Could not query daily volumes: {e}", e=exc)
            return {}

    def _load_consecutive_failures_from_db(self) -> dict[str, int]:
        """Return {url: consecutive_failure_count} from feed_sources."""
        if self._engine is None:
            return {}
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT url, last_error "
                        "FROM feed_sources WHERE is_active = TRUE"
                    )
                ).fetchall()
            # last_error != NULL → at least 1 prior failure; NULL → 0.
            return {row.url: (1 if row.last_error else 0) for row in rows}
        except Exception as exc:
            logger.debug("Could not load feed failure history: {e}", e=exc)
            return {}

    def _persist_health(self, results: list[FeedHealth]) -> None:
        """Update last_fetched_at and last_error in feed_sources."""
        if self._engine is None:
            return
        try:
            with self._engine.begin() as conn:
                for h in results:
                    conn.execute(
                        text(
                            """
                            UPDATE feed_sources
                            SET last_fetched_at = :now,
                                last_error = :err,
                                updated_at  = :now
                            WHERE url = :url
                        """
                        ),
                        {
                            "url": h.url,
                            "now": h.checked_at,
                            "err": h.error_message if not h.is_available else None,
                        },
                    )
        except Exception as exc:
            logger.debug("Could not persist feed health: {e}", e=exc)

    def build_report(
        self,
        health_report: FeedHealthReport,
        daily_volumes: dict[str, int],
    ) -> dict:
        """Return a JSON-serializable health summary for inclusion in run_report."""
        return {
            "total_feeds": health_report.total_feeds,
            "available_feeds": health_report.available_feeds,
            "unavailable_feeds": health_report.unavailable_feeds,
            "avg_response_time_ms": health_report.avg_response_time_ms,
            "unhealthy_feeds": health_report.unhealthy,
            "per_feed": [
                {
                    "name": h.name,
                    "url": h.url,
                    "available": h.is_available,
                    "http_status": h.http_status,
                    "response_time_ms": h.response_time_ms,
                    "consecutive_failures": h.consecutive_failures,
                    "daily_volume": daily_volumes.get(h.url, 0),
                    "error_message": h.error_message,
                }
                for h in health_report.feeds
            ],
        }
