"""Resilience tests — behavior when PostgreSQL is slow or unavailable."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from mapear_domain.models.base import DiscoveredURL
from mapear_rss.discovery.url_frontier import URLFrontier
from mapear_rss.transformation.deduplicator import Deduplicator


class TestFrontierPGUnavailable:
    """URL Frontier behavior when PostgreSQL is unreachable."""

    def test_add_urls_raises_on_connection_error(self) -> None:
        """Frontier.add_urls() should propagate connection errors."""
        mock_engine = MagicMock()
        mock_engine.begin.side_effect = OperationalError(
            "connection refused", {}, Exception()
        )

        frontier = URLFrontier(engine=mock_engine)

        with pytest.raises(OperationalError):
            frontier.add_urls(
                [
                    DiscoveredURL(
                        url="https://example.com/test",
                        source_feed="https://example.com/feed",
                    )
                ]
            )

    def test_get_stats_raises_on_timeout(self) -> None:
        """Frontier.get_stats() should propagate timeout."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = OperationalError("timeout", {}, Exception())
        mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        frontier = URLFrontier(engine=mock_engine)

        with pytest.raises(OperationalError):
            frontier.get_stats()

    def test_mark_failed_batch_empty_is_noop(self) -> None:
        """Batch operations with empty lists should not touch DB."""
        mock_engine = MagicMock()
        frontier = URLFrontier(engine=mock_engine)

        # Should not call engine at all
        frontier.mark_failed_batch([])
        frontier.mark_completed_batch([])
        mock_engine.begin.assert_not_called()


class TestDeduplicatorPGUnavailable:
    """Deduplicator cross-batch dedup when PG is down."""

    def test_load_hashes_with_no_engine(self) -> None:
        """Deduplicator without engine should skip cross-batch load."""
        dedup = Deduplicator(engine=None)
        dedup.load_existing_hashes()
        assert dedup.known_hashes == set()

    def test_load_hashes_pg_error_propagates(self) -> None:
        """Deduplicator should propagate PG errors during hash load."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = OperationalError(
            "connection refused", {}, Exception()
        )
        mock_engine.connect.return_value.__enter__ = lambda s: mock_conn
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        dedup = Deduplicator(engine=mock_engine)
        with pytest.raises(OperationalError):
            dedup.load_existing_hashes()

    def test_deduplicate_works_without_cross_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dedup should still remove intra-batch duplicates without PG."""
        from datetime import UTC, datetime

        from mapear_domain.models.base import RawArticle
        from mapear_rss.extraction.content_hasher import hash_content

        monkeypatch.setenv("ENRICHMENT_MODE", "skip")

        article = RawArticle(
            url="https://example.com/test",
            source_feed="test_feed",
            title="Test Article",
            content="A" * 200,
            published_at=datetime.now(UTC),
            content_hash=hash_content("Test Article", "A" * 200),
        )

        dedup = Deduplicator(engine=None)
        result = dedup.deduplicate([article, article])

        # Should remove the duplicate even without PG
        assert len(result) == 1


class TestScraperWithPGTimeout:
    """Scraper behavior when frontier DB is slow."""

    def test_scraper_independent_of_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scraper itself doesn't depend on PG — only frontier does."""
        monkeypatch.setenv("ENRICHMENT_MODE", "skip")
        monkeypatch.setenv("SCRAPER_DELAY_MIN", "0")
        monkeypatch.setenv("SCRAPER_DELAY_MAX", "0")

        from mapear_rss.extraction.scraper import Scraper

        mock_cb = MagicMock()
        mock_cb.is_allowed.return_value = True

        scraper = Scraper(circuit_breaker=mock_cb)
        # Scraper can be instantiated without DB
        assert scraper is not None
        scraper.close()
