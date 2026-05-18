"""RSS feed reader using feedparser.

Fetches and parses RSS/Atom feeds, returning discovered article URLs
with metadata for the URL Frontier.
"""

from datetime import UTC, datetime

import feedparser
import httpx
from loguru import logger

from mapear_domain.models.base import DiscoveredURL
from mapear_infra.retry import retry_on_network_error
from mapear_rss.config import get_feed_headers, get_rss_settings
from mapear_rss.discovery.rn_filter import matches as rn_matches


class RSSReader:
    """Reads RSS/Atom feeds and extracts article URLs."""

    def __init__(self) -> None:
        settings = get_rss_settings()
        self.user_agent = settings.scraper.user_agent
        self.timeout = 30.0
        self._headers = get_feed_headers(self.user_agent)

    @retry_on_network_error(max_attempts=3)
    def fetch_feed(
        self,
        feed_url: str,
        rn_focused: bool = False,
        min_published_at: datetime | None = None,
    ) -> list[DiscoveredURL]:
        """Fetch an RSS feed and return discovered URLs.

        Args:
            feed_url: The URL of the RSS/Atom feed.
            rn_focused: If True, bypass the RN keyword filter — the whole
                feed is treated as RN-relevant (e.g. Tribuna do Norte, G1 RN).
                General-interest feeds (Folha, Estadão, G1 nacional) leave
                this as False so only entries mentioning RN cities / mayors /
                governor / state sigla get enqueued (BL-08).
            min_published_at: Watermark cutoff. When set, entries with no
                date or with published_at < cutoff are skipped entirely so
                historical items never enter the frontier.

        Returns:
            List of DiscoveredURL objects with metadata.
        """
        logger.info("Fetching feed: {url}", url=feed_url)

        response = httpx.get(
            feed_url,
            headers=self._headers,
            timeout=self.timeout,
            follow_redirects=True,
        )
        logger.info(
            "Feed response: {url} → HTTP {status}",
            url=feed_url,
            status=response.status_code,
        )
        response.raise_for_status()

        feed = feedparser.parse(response.text)

        if feed.bozo and not feed.entries:
            logger.warning(
                "Feed parse error for {url}: {error}",
                url=feed_url,
                error=str(feed.bozo_exception),
            )
            return []

        urls = []
        filtered_out = 0
        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue

            title = entry.get("title")
            description = entry.get("description") or entry.get("summary")

            if not rn_focused and not rn_matches(title, description):
                filtered_out += 1
                continue

            published_at = self._parse_date(entry)

            if min_published_at is not None and (
                published_at is None or published_at < min_published_at
            ):
                filtered_out += 1
                continue

            try:
                discovered = DiscoveredURL(
                    url=link,
                    source_feed=feed_url,
                    title=title,
                    published_at=published_at,
                )
                urls.append(discovered)
            except Exception as e:
                logger.debug(
                    "Skipping invalid entry from {url}: {error}",
                    url=feed_url,
                    error=str(e),
                )

        logger.info(
            "Discovered {count} URLs from {url} (filtered {dropped}, "
            "rn_focused={focused}, min_published_at={cutoff})",
            count=len(urls),
            url=feed_url,
            dropped=filtered_out,
            focused=rn_focused,
            cutoff=min_published_at.isoformat() if min_published_at else "none",
        )
        return urls

    def fetch_multiple(
        self,
        feed_urls: list[str],
        rn_focused_feeds: set[str] | None = None,
        min_published_at: datetime | None = None,
    ) -> list[DiscoveredURL]:
        """Fetch multiple feeds and aggregate results.

        Args:
            feed_urls: Feed URLs to fetch.
            rn_focused_feeds: URLs in this set bypass the RN keyword filter
                (see ``fetch_feed``). ``None`` is treated as empty set.
            min_published_at: Watermark cutoff passed to each ``fetch_feed``
                call — entries older than this are silently dropped.

        Continues on failure — failed feeds are logged and skipped.
        """
        rn_focused_feeds = rn_focused_feeds or set()
        all_urls: list[DiscoveredURL] = []
        failed_feeds: list[str] = []

        for url in feed_urls:
            try:
                urls = self.fetch_feed(
                    url,
                    rn_focused=url in rn_focused_feeds,
                    min_published_at=min_published_at,
                )
                all_urls.extend(urls)
            except Exception as e:
                failed_feeds.append(url)
                logger.error(
                    "Failed to fetch feed {url}: {error}",
                    url=url,
                    error=str(e),
                )

        logger.info(
            "Total discovered: {count} URLs from {feeds} feeds ({failed} feeds failed)",
            count=len(all_urls),
            feeds=len(feed_urls),
            failed=len(failed_feeds),
        )
        return all_urls

    @staticmethod
    def _parse_date(entry: dict) -> datetime | None:
        """Parse published date from a feed entry.

        Fallback chain:
        1. entry.published_parsed / entry.updated_parsed (feedparser tuple)
        2. entry.published / entry.updated (raw string via dateutil)
        """
        # Try feedparser's pre-parsed date tuples first
        for field in ("published_parsed", "updated_parsed"):
            parsed = entry.get(field)
            if parsed:
                try:
                    from time import mktime

                    return datetime.fromtimestamp(mktime(parsed), tz=UTC)
                except (TypeError, ValueError, OverflowError):
                    continue

        # Fallback: parse raw date strings with dateutil
        for field in ("published", "updated", "pubDate"):
            raw = entry.get(field)
            if raw and isinstance(raw, str):
                try:
                    from dateutil.parser import parse as dateutil_parse

                    dt = dateutil_parse(raw, fuzzy=True)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    return dt
                except (ValueError, TypeError, OverflowError):
                    continue

        return None
