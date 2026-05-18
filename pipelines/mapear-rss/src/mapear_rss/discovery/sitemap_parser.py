"""Sitemap parser for discovering article URLs from XML sitemaps.

Supports both sitemap index files and regular sitemaps.
Useful as a complement to RSS for portals that publish sitemaps
with more complete URL coverage.
"""

from datetime import datetime

import httpx
from loguru import logger

from mapear_domain.models.base import DiscoveredURL
from mapear_infra.retry import retry_on_network_error
from mapear_rss.config import get_feed_headers, get_rss_settings

try:
    from xml.etree import ElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


class SitemapParser:
    """Parses XML sitemaps to discover article URLs."""

    def __init__(self) -> None:
        settings = get_rss_settings()
        self.user_agent = settings.scraper.user_agent
        self.timeout = 30.0
        self._headers = get_feed_headers(self.user_agent)

    @retry_on_network_error(max_attempts=3)
    def fetch_sitemap(
        self,
        sitemap_url: str,
        source_feed: str = "",
    ) -> list[DiscoveredURL]:
        """Fetch and parse a sitemap, returning discovered URLs.

        Handles sitemap index files recursively.

        Args:
            sitemap_url: URL of the sitemap XML.
            source_feed: Identifier for this source.

        Returns:
            List of DiscoveredURL objects.
        """
        source = source_feed or sitemap_url
        logger.info("Fetching sitemap: {url}", url=sitemap_url)

        response = httpx.get(
            sitemap_url,
            headers=self._headers,
            timeout=self.timeout,
            follow_redirects=True,
        )
        response.raise_for_status()

        root = ET.fromstring(response.text)

        # Sitemap index — recursivamente busca sub-sitemaps
        if root.tag == f"{{{SITEMAP_NS['sm']}}}sitemapindex":
            return self._parse_sitemap_index(root, source)

        # Sitemap regular
        return self._parse_urlset(root, source)

    def _parse_sitemap_index(
        self, root: ET.Element, source: str
    ) -> list[DiscoveredURL]:
        """Parse a sitemap index and recurse into child sitemaps."""
        urls: list[DiscoveredURL] = []

        for sitemap in root.findall("sm:sitemap", SITEMAP_NS):
            loc = sitemap.find("sm:loc", SITEMAP_NS)
            if loc is not None and loc.text:
                try:
                    child_urls = self.fetch_sitemap(loc.text, source)
                    urls.extend(child_urls)
                except Exception as e:
                    logger.warning(
                        "Failed to fetch child sitemap {url}: {error}",
                        url=loc.text,
                        error=str(e),
                    )

        return urls

    def _parse_urlset(self, root: ET.Element, source: str) -> list[DiscoveredURL]:
        """Parse a regular sitemap urlset."""
        urls: list[DiscoveredURL] = []

        for url_elem in root.findall("sm:url", SITEMAP_NS):
            loc = url_elem.find("sm:loc", SITEMAP_NS)
            if loc is None or not loc.text:
                continue

            lastmod = url_elem.find("sm:lastmod", SITEMAP_NS)
            published_at = None
            if lastmod is not None and lastmod.text:
                import contextlib

                with contextlib.suppress(ValueError):
                    published_at = datetime.fromisoformat(
                        lastmod.text.replace("Z", "+00:00")
                    )

            try:
                discovered = DiscoveredURL(
                    url=loc.text,
                    source_feed=source,
                    published_at=published_at,
                )
                urls.append(discovered)
            except Exception as e:
                logger.debug(
                    "Skipping invalid sitemap URL {url}: {error}",
                    url=loc.text,
                    error=str(e),
                )

        logger.info(
            "Parsed {count} URLs from sitemap",
            count=len(urls),
        )
        return urls
