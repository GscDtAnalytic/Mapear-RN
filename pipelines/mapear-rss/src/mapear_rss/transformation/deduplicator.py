"""Content deduplication using content hashes and canonical URLs.

Detects duplicate content both within a batch and against
previously processed items (via URL Frontier content_hash).
Also deduplicates by canonical URL to catch the same article
syndicated with different tracking parameters across feeds.
"""

from loguru import logger
from sqlalchemy import text
from sqlalchemy.engine import Engine

from mapear_domain.models.base import RawArticle
from mapear_storage.transformation.url_canonicalizer import canonicalize_url


class Deduplicator:
    """Removes duplicate articles based on content_hash and canonical URL."""

    def __init__(
        self,
        known_hashes: set[str] | None = None,
        engine: Engine | None = None,
    ) -> None:
        self.known_hashes: set[str] = known_hashes or set()
        self._known_canonical_urls: set[str] = set()
        self._engine = engine

    def load_existing_hashes(self) -> None:
        """Load content_hash values from url_frontier (cross-batch dedup)."""
        if self._engine is None:
            return

        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT content_hash FROM url_frontier "
                    "WHERE content_hash IS NOT NULL "
                    "AND status = 'completed'"
                )
            ).fetchall()

        loaded = {row[0] for row in rows}
        self.known_hashes.update(loaded)
        logger.info(
            "Loaded {count} existing hashes for cross-batch dedup",
            count=len(loaded),
        )

    def load_existing_canonical_urls(self) -> None:
        """Load canonical URLs from url_frontier for URL-based cross-feed dedup.

        Catches cases where the same article is linked from multiple feeds
        with different tracking parameters (e.g., ?utm_source=feed-a vs
        ?utm_source=feed-b) but resolves to the same canonical form.
        """
        if self._engine is None:
            return

        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT DISTINCT url FROM url_frontier WHERE status = 'completed'")
            ).fetchall()

        for row in rows:
            self._known_canonical_urls.add(canonicalize_url(row[0]))

        logger.info(
            "Loaded {count} canonical URLs for cross-feed URL dedup",
            count=len(self._known_canonical_urls),
        )

    def deduplicate(self, articles: list[RawArticle]) -> list[RawArticle]:
        """Remove duplicates from a batch of articles.

        Checks both content_hash (detects same text from different URLs) and
        canonical URL (detects same URL with different tracking parameters).
        """
        unique: list[RawArticle] = []
        hash_dupes = 0
        url_dupes = 0

        for article in articles:
            if article.content_hash in self.known_hashes:
                hash_dupes += 1
                logger.debug(
                    "Duplicate (hash): {url}",
                    url=str(article.url),
                )
                continue

            canonical = canonicalize_url(str(article.url))
            if canonical in self._known_canonical_urls:
                url_dupes += 1
                logger.debug(
                    "Duplicate (canonical URL): {url} → {canonical}",
                    url=str(article.url),
                    canonical=canonical,
                )
                continue

            self.known_hashes.add(article.content_hash)
            self._known_canonical_urls.add(canonical)
            unique.append(article)

        total_dupes = hash_dupes + url_dupes
        if total_dupes > 0:
            logger.info(
                "Deduplication: {unique} unique, {dupes} duplicates removed "
                "(hash={h}, url={u})",
                unique=len(unique),
                dupes=total_dupes,
                h=hash_dupes,
                u=url_dupes,
            )

        return unique
