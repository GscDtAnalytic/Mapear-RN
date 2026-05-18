"""URL Frontier — manages the queue of URLs to be extracted.

Uses PostgreSQL as the backing store. Tracks URL status
(pending → in_progress → completed/failed) and prevents
re-extraction of already-processed URLs.
"""

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine

from mapear_domain.models.base import DiscoveredURL
from mapear_infra.config import get_settings


class URLFrontier:
    """Manages the URL extraction queue in PostgreSQL."""

    def __init__(self, engine: Engine | None = None) -> None:
        if engine is not None:
            self.engine = engine
        else:
            settings = get_settings()
            self.engine = create_engine(
                settings.postgres.dsn,
                pool_size=settings.postgres.pool_size,
                max_overflow=settings.postgres.max_overflow,
            )

    def add_urls(self, urls: list[DiscoveredURL]) -> int:
        """Add discovered URLs to the frontier, skipping duplicates.

        Returns:
            Number of new URLs actually inserted.
        """
        if not urls:
            return 0

        inserted = 0
        with self.engine.begin() as conn:
            for url in urls:
                result = conn.execute(
                    text(
                        """
                        INSERT INTO url_frontier (url, source_feed, title, published_at)
                        VALUES (:url, :source_feed, :title, :published_at)
                        ON CONFLICT (url) DO NOTHING
                    """
                    ),
                    {
                        "url": str(url.url),
                        "source_feed": url.source_feed,
                        "title": url.title,
                        "published_at": url.published_at,
                    },
                )
                inserted += result.rowcount

        logger.info(
            "Added {inserted}/{total} new URLs to frontier",
            inserted=inserted,
            total=len(urls),
        )
        return inserted

    def get_pending(self, limit: int = 100) -> list[dict]:
        """Fetch pending URLs and mark them as in_progress.

        Uses SELECT ... FOR UPDATE SKIP LOCKED for safe concurrent access.

        Returns:
            List of dicts with url, source_feed, title, published_at.
        """
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    UPDATE url_frontier
                    SET status = 'in_progress',
                        last_attempt_at = :now,
                        attempt_count = attempt_count + 1,
                        updated_at = :now
                    WHERE id IN (
                        SELECT id FROM url_frontier
                        WHERE status = 'pending'
                        ORDER BY published_at DESC NULLS LAST
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING url, source_feed, title, published_at
                """
                ),
                {"limit": limit, "now": datetime.now(UTC)},
            ).fetchall()

        results = [
            {
                "url": row.url,
                "source_feed": row.source_feed,
                "title": row.title,
                "published_at": row.published_at,
            }
            for row in rows
        ]

        logger.info("Fetched {count} pending URLs", count=len(results))
        return results

    def mark_completed(self, url: str, content_hash: str) -> None:
        """Mark a URL as successfully extracted."""
        self.mark_completed_batch([(url, content_hash)])

    def mark_completed_batch(self, items: list[tuple[str, str]]) -> None:
        """Mark multiple URLs as completed in a single transaction."""
        if not items:
            return
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE url_frontier
                    SET status = 'completed',
                        content_hash = :content_hash,
                        updated_at = :now
                    WHERE url = :url
                """
                ),
                [{"url": url, "content_hash": ch, "now": now} for url, ch in items],
            )
        logger.debug("Batch marked {count} URLs as completed", count=len(items))

    def mark_failed(self, url: str) -> None:
        """Mark a URL as failed extraction."""
        self.mark_failed_batch([url])

    def mark_failed_batch(self, urls: list[str]) -> None:
        """Mark multiple URLs as failed in a single transaction."""
        if not urls:
            return
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE url_frontier
                    SET status = 'failed', updated_at = :now
                    WHERE url = :url
                """
                ),
                [{"url": url, "now": now} for url in urls],
            )
        logger.debug("Batch marked {count} URLs as failed", count=len(urls))

    def add_to_dlq(
        self,
        url: str,
        source_feed: str,
        error_type: str,
        error_message: str,
        stage: str,
    ) -> None:
        """Add a failed article to the Dead Letter Queue."""
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO failed_articles
                        (url, source_feed, error_type, error_message, stage,
                         last_failure_at)
                    VALUES
                        (:url, :source_feed, :error_type, :error_message,
                         :stage, :now)
                    ON CONFLICT (url, stage) DO UPDATE SET
                        retry_count = failed_articles.retry_count + 1,
                        error_message = :error_message,
                        last_failure_at = :now
                """
                ),
                {
                    "url": url,
                    "source_feed": source_feed,
                    "error_type": error_type,
                    "error_message": error_message,
                    "stage": stage,
                    "now": now,
                },
            )

    def get_retryable(
        self,
        max_retries: int = 3,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch failed URLs eligible for retry with exponential backoff.

        Only returns URLs where:
        - status = 'failed'
        - attempt_count < max_retries
        - enough time has passed (2^attempt_count minutes since last attempt)
        """
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    UPDATE url_frontier
                    SET status = 'in_progress',
                        last_attempt_at = :now,
                        attempt_count = attempt_count + 1,
                        updated_at = :now
                    WHERE id IN (
                        SELECT id FROM url_frontier
                        WHERE status = 'failed'
                          AND attempt_count < :max_retries
                        ORDER BY attempt_count ASC, updated_at ASC
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING url, source_feed, title, published_at,
                              attempt_count
                """
                ),
                {"now": now, "max_retries": max_retries, "limit": limit},
            ).fetchall()

        results = [
            {
                "url": row.url,
                "source_feed": row.source_feed,
                "title": row.title,
                "published_at": row.published_at,
                "attempt": row.attempt_count,
            }
            for row in rows
        ]
        if results:
            logger.info(
                "Fetched {count} retryable URLs",
                count=len(results),
            )
        return results

    def mark_deferred_batch(self, urls: list[str]) -> None:
        """Reset deferred (cooldown-skipped) URLs back to pending.

        URLs deferred by domain cooldown were marked in_progress by
        get_pending() but never completed or failed. Reverting them
        to pending lets the next run retry them.
        """
        if not urls:
            return
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE url_frontier
                    SET status = 'pending',
                        attempt_count = GREATEST(attempt_count - 1, 0),
                        updated_at = :now
                    WHERE url = :url AND status = 'in_progress'
                """
                ),
                [{"url": url, "now": now} for url in urls],
            )
        logger.debug("Reset {count} deferred URLs back to pending", count=len(urls))

    def reset_stale_in_progress(self, timeout_minutes: int = 30) -> int:
        """Reset URLs stuck in 'in_progress' back to 'pending'."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=timeout_minutes)
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE url_frontier
                    SET status = 'pending', updated_at = :now
                    WHERE status = 'in_progress'
                      AND last_attempt_at < :cutoff
                """
                ),
                {"now": now, "cutoff": cutoff},
            )
        count = result.rowcount
        if count > 0:
            logger.warning(
                "Reset {count} stale in_progress URLs",
                count=count,
            )
        return count

    def recirculate_stale(
        self,
        ttl_hours: int,
        limit: int,
        include_failed: bool = False,
    ) -> int:
        """Re-enable aged completed/failed URLs so they get reprocessed.

        This exists to break frontier starvation: when discovery keeps
        returning URLs the frontier has already seen (status completed
        or failed), ``get_pending`` returns 0 and the run does nothing.
        Recirculation moves a bounded number of URLs back to
        ``pending`` once they have aged past ``ttl_hours``.

        ``recirculated_at`` is a one-shot guard: a URL cannot be
        recirculated twice within the same TTL window, which prevents
        a hot loop on small frontiers.

        Returns the number of URLs actually re-pended.
        """
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=ttl_hours)
        statuses: tuple[str, ...] = (
            ("completed", "failed") if include_failed else ("completed",)
        )

        stmt = text(
            """
            UPDATE url_frontier
            SET status = 'pending',
                recirculated_at = :now,
                updated_at = :now
            WHERE id IN (
                SELECT id FROM url_frontier
                WHERE status IN :statuses
                  AND updated_at < :cutoff
                  AND (recirculated_at IS NULL OR recirculated_at < :cutoff)
                ORDER BY updated_at ASC
                LIMIT :limit
            )
            """
        ).bindparams(bindparam("statuses", expanding=True))

        with self.engine.begin() as conn:
            result = conn.execute(
                stmt,
                {
                    "now": now,
                    "cutoff": cutoff,
                    "statuses": list(statuses),
                    "limit": limit,
                },
            )
            count = result.rowcount or 0

        if count > 0:
            logger.warning(
                "Recirculated {count} stale URLs back to pending "
                "(ttl_hours={ttl}, include_failed={fail})",
                count=count,
                ttl=ttl_hours,
                fail=include_failed,
            )
        return count

    def purge_old_pending(self, ttl_days: int = 14) -> int:
        """Delete pending/failed URLs whose published_at is older than ttl_days.

        Prevents stale articles from occupying the frontier indefinitely when
        the blocked-domain cooldown keeps them from ever being extracted.
        """
        cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    DELETE FROM url_frontier
                    WHERE status IN ('pending', 'failed')
                      AND published_at IS NOT NULL
                      AND published_at < :cutoff
                    """
                ),
                {"cutoff": cutoff},
            )
        count = result.rowcount or 0
        if count > 0:
            logger.info(
                "Purged {count} stale frontier URLs older than {days}d",
                count=count,
                days=ttl_days,
            )
        return count

    def get_stats(self) -> dict[str, int]:
        """Return counts by status for monitoring."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT status, COUNT(*) AS cnt
                    FROM url_frontier
                    GROUP BY status
                """
                )
            ).fetchall()

        return {row.status: row.cnt for row in rows}
