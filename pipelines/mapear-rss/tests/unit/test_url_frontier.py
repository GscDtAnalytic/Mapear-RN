"""Tests for URL Frontier using in-memory SQLite."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from mapear_domain.models.base import DiscoveredURL


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine with frontier schema."""
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(
            text(
                """
            CREATE TABLE url_frontier (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                source_feed TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                title TEXT,
                published_at TIMESTAMP,
                discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_attempt_at TIMESTAMP,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT,
                recirculated_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """
            )
        )
        conn.execute(
            text(
                """
            CREATE TABLE failed_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                source_feed TEXT,
                error_type TEXT NOT NULL,
                error_message TEXT,
                stage TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                first_failure_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_failure_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP,
                UNIQUE (url, stage)
            )
        """
            )
        )
    return eng


@pytest.fixture
def frontier(engine, monkeypatch):
    """Create a URLFrontier with the test engine."""
    monkeypatch.setenv("ENRICHMENT_MODE", "skip")
    from mapear_rss.discovery.url_frontier import URLFrontier

    return URLFrontier(engine=engine)


@pytest.fixture
def sample_urls() -> list[DiscoveredURL]:
    return [
        DiscoveredURL(
            url="https://tribunadonorte.com.br/noticia-1",
            source_feed="https://tribunadonorte.com.br/feed/",
            title="Notícia 1",
            published_at=datetime.now(UTC),
        ),
        DiscoveredURL(
            url="https://g1.globo.com/rn/noticia-2",
            source_feed="https://g1.globo.com/rn/rss2.xml",
            title="Notícia 2",
        ),
    ]


class TestURLFrontier:
    def test_add_urls_inserts(self, frontier, sample_urls, engine) -> None:
        inserted = frontier.add_urls(sample_urls)
        assert inserted == 2

        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM url_frontier")).scalar()
        assert count == 2

    def test_add_urls_skips_duplicates(self, frontier, sample_urls) -> None:
        frontier.add_urls(sample_urls)
        inserted = frontier.add_urls(sample_urls)
        assert inserted == 0

    def test_add_urls_empty_list(self, frontier) -> None:
        assert frontier.add_urls([]) == 0

    def test_get_pending_returns_urls(self, frontier, sample_urls) -> None:
        frontier.add_urls(sample_urls)

        # SQLite doesn't support FOR UPDATE SKIP LOCKED, so we test the
        # frontier methods that don't use that syntax directly
        with frontier.engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT url, source_feed FROM url_frontier "
                    "WHERE status = 'pending'"
                )
            ).fetchall()

        assert len(rows) == 2

    def test_mark_completed(self, frontier, sample_urls, engine) -> None:
        frontier.add_urls(sample_urls)
        frontier.mark_completed(
            "https://tribunadonorte.com.br/noticia-1",
            "abc123hash",
        )

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status, content_hash FROM url_frontier WHERE url = :url"),
                {"url": "https://tribunadonorte.com.br/noticia-1"},
            ).fetchone()

        assert row.status == "completed"
        assert row.content_hash == "abc123hash"

    def test_mark_completed_batch(self, frontier, sample_urls, engine) -> None:
        frontier.add_urls(sample_urls)
        frontier.mark_completed_batch(
            [
                ("https://tribunadonorte.com.br/noticia-1", "hash1"),
                ("https://g1.globo.com/rn/noticia-2", "hash2"),
            ]
        )

        with engine.connect() as conn:
            completed = conn.execute(
                text("SELECT COUNT(*) FROM url_frontier WHERE status = 'completed'")
            ).scalar()
        assert completed == 2

    def test_mark_failed(self, frontier, sample_urls, engine) -> None:
        frontier.add_urls(sample_urls)
        frontier.mark_failed("https://g1.globo.com/rn/noticia-2")

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status FROM url_frontier WHERE url = :url"),
                {"url": "https://g1.globo.com/rn/noticia-2"},
            ).fetchone()

        assert row.status == "failed"

    def test_mark_failed_batch(self, frontier, sample_urls, engine) -> None:
        frontier.add_urls(sample_urls)
        frontier.mark_failed_batch(
            [
                "https://tribunadonorte.com.br/noticia-1",
                "https://g1.globo.com/rn/noticia-2",
            ]
        )

        with engine.connect() as conn:
            failed = conn.execute(
                text("SELECT COUNT(*) FROM url_frontier WHERE status = 'failed'")
            ).scalar()
        assert failed == 2

    def test_add_to_dlq(self, frontier, engine) -> None:
        frontier.add_to_dlq(
            url="https://example.com/broken",
            source_feed="https://example.com/feed",
            error_type="extraction_failed",
            error_message="Connection timeout",
            stage="extraction",
        )

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM failed_articles WHERE url = :url"),
                {"url": "https://example.com/broken"},
            ).fetchone()

        assert row is not None
        assert row.error_type == "extraction_failed"
        assert row.stage == "extraction"

    def test_get_stats(self, frontier, sample_urls) -> None:
        frontier.add_urls(sample_urls)
        frontier.mark_completed("https://tribunadonorte.com.br/noticia-1", "hash1")

        stats = frontier.get_stats()
        assert stats.get("completed", 0) == 1
        assert stats.get("pending", 0) == 1


class TestRecirculation:
    def _backdate(self, engine, url: str, hours: int) -> None:
        """Force an url_frontier row to look aged."""
        old = datetime.now(UTC) - timedelta(hours=hours)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE url_frontier SET updated_at = :old, "
                    "recirculated_at = NULL WHERE url = :url"
                ),
                {"old": old, "url": url},
            )

    def test_recirculates_aged_completed(self, frontier, sample_urls, engine) -> None:
        frontier.add_urls(sample_urls)
        frontier.mark_completed_batch(
            [
                ("https://tribunadonorte.com.br/noticia-1", "h1"),
                ("https://g1.globo.com/rn/noticia-2", "h2"),
            ]
        )
        self._backdate(engine, "https://tribunadonorte.com.br/noticia-1", hours=100)
        self._backdate(engine, "https://g1.globo.com/rn/noticia-2", hours=100)

        count = frontier.recirculate_stale(ttl_hours=72, limit=10)
        assert count == 2
        stats = frontier.get_stats()
        assert stats.get("pending", 0) == 2

    def test_does_not_recirculate_fresh(self, frontier, sample_urls, engine) -> None:
        frontier.add_urls(sample_urls)
        frontier.mark_completed_batch(
            [("https://tribunadonorte.com.br/noticia-1", "h1")]
        )
        # updated_at is "now" — well inside the 72h TTL
        count = frontier.recirculate_stale(ttl_hours=72, limit=10)
        assert count == 0

    def test_idempotent_within_ttl(self, frontier, sample_urls, engine) -> None:
        frontier.add_urls(sample_urls)
        frontier.mark_completed_batch(
            [("https://tribunadonorte.com.br/noticia-1", "h1")]
        )
        self._backdate(engine, "https://tribunadonorte.com.br/noticia-1", hours=100)

        first = frontier.recirculate_stale(ttl_hours=72, limit=10)
        assert first == 1
        # Mark it completed again so it becomes a candidate by status,
        # but recirculated_at is now fresh — still inside TTL.
        frontier.mark_completed("https://tribunadonorte.com.br/noticia-1", "h1")
        second = frontier.recirculate_stale(ttl_hours=72, limit=10)
        assert second == 0  # guard prevents loop

    def test_include_failed_opt_in(self, frontier, sample_urls, engine) -> None:
        frontier.add_urls(sample_urls)
        frontier.mark_failed_batch(
            [
                "https://tribunadonorte.com.br/noticia-1",
                "https://g1.globo.com/rn/noticia-2",
            ]
        )
        self._backdate(engine, "https://tribunadonorte.com.br/noticia-1", hours=100)
        self._backdate(engine, "https://g1.globo.com/rn/noticia-2", hours=100)

        # include_failed=False → no-op on failed rows
        assert frontier.recirculate_stale(ttl_hours=72, limit=10) == 0
        # include_failed=True → both recirculated
        assert (
            frontier.recirculate_stale(ttl_hours=72, limit=10, include_failed=True) == 2
        )


class TestPurgeOldPending:
    def _insert_with_published(
        self, engine, url: str, status: str, days_ago: int
    ) -> None:
        pub = datetime.now(UTC) - timedelta(days=days_ago)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO url_frontier (url, source_feed, status, published_at) "
                    "VALUES (:url, 'feed', :status, :pub)"
                ),
                {"url": url, "status": status, "pub": pub},
            )

    def test_purges_old_pending_and_failed(self, frontier, engine) -> None:
        self._insert_with_published(engine, "https://old.com/1", "pending", days_ago=20)
        self._insert_with_published(engine, "https://old.com/2", "failed", days_ago=20)
        self._insert_with_published(engine, "https://new.com/3", "pending", days_ago=5)

        purged = frontier.purge_old_pending(ttl_days=14)

        assert purged == 2
        stats = frontier.get_stats()
        assert stats.get("pending", 0) == 1
        assert stats.get("failed", 0) == 0

    def test_does_not_purge_completed(self, frontier, engine) -> None:
        self._insert_with_published(
            engine, "https://done.com/1", "completed", days_ago=20
        )

        purged = frontier.purge_old_pending(ttl_days=14)

        assert purged == 0

    def test_does_not_purge_null_published_at(self, frontier, engine) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO url_frontier (url, source_feed, status) "
                    "VALUES ('https://nodates.com/1', 'feed', 'pending')"
                )
            )

        purged = frontier.purge_old_pending(ttl_days=1)

        assert purged == 0
        assert frontier.get_stats().get("pending", 0) == 1
