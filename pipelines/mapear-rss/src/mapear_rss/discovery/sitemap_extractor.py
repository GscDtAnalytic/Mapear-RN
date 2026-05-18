"""Backfill sitemap extractor — discovers article URLs from XML sitemaps.

Extends the basic ``SitemapParser`` with:
- robots.txt-based sitemap discovery
- Recursive sitemap-index support (max depth 3)
- lastmod-based date filtering
- Article URL heuristics (excludes listing/tag/author pages)

Designed for one-shot backfill runs, not the regular RSS pipeline.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
from loguru import logger

from mapear_domain.models.base import DiscoveredURL

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_TIMEOUT = 30.0
_MAX_DEPTH = 3

_LISTING_PATTERNS: tuple[str, ...] = (
    "/categoria/",
    "/category/",
    "/tag/",
    "/tags/",
    "/autor/",
    "/author/",
    "/page/",
    "/busca",
    "/search",
    "/feed/",
    "/rss",
    "/sitemap",
)

_DATE_PATH_RE = re.compile(r"/\d{4}/\d{2}/")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/xml,application/xml,*/*;q=0.5",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


@dataclass
class ArticleURL:
    url: str
    domain: str
    lastmod: date | None = None
    source_sitemap: str = ""

    def to_discovered(self) -> DiscoveredURL:
        published_at: datetime | None = None
        if self.lastmod is not None:
            published_at = datetime(
                self.lastmod.year,
                self.lastmod.month,
                self.lastmod.day,
                tzinfo=UTC,
            )
        return DiscoveredURL(
            url=self.url,
            source_feed=self.source_sitemap or self.domain,
            published_at=published_at,
        )


def _is_article_url(url: str) -> bool:
    """Heuristic: return True when url looks like a leaf article page."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    if parsed.query:
        return False

    for pat in _LISTING_PATTERNS:
        if pat in path:
            return False

    # Accept if path has date pattern OR >= 3 segments OR long final slug
    if _DATE_PATH_RE.search(path):
        return True
    segments = [s for s in path.split("/") if s]
    if len(segments) >= 3:
        return True
    return bool(segments and len(segments[-1]) > 20)


def _parse_lastmod(text: str) -> date | None:
    with contextlib.suppress(ValueError, AttributeError):
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.date()
    with contextlib.suppress(ValueError, AttributeError):
        return date.fromisoformat(text[:10])
    return None


@dataclass
class SitemapExtractor:
    """Discovers article URLs from a domain's XML sitemaps."""

    timeout: float = _TIMEOUT
    max_depth: int = _MAX_DEPTH
    _visited: set[str] = field(default_factory=set, init=False)

    def extract_from_domain(
        self,
        domain: str,
        since: date,
    ) -> list[ArticleURL]:
        """Discover sitemap URLs for ``domain`` and filter to articles since ``since``.

        Discovery order: /sitemap.xml → /sitemap_index.xml → robots.txt.
        """
        self._visited.clear()
        base = f"https://{domain}"

        sitemap_candidates = [
            f"{base}/sitemap.xml",
            f"{base}/sitemap_index.xml",
        ]

        # robots.txt may advertise additional sitemap URLs
        robots_sitemaps = self._sitemaps_from_robots(base)
        sitemap_candidates.extend(robots_sitemaps)

        all_urls: list[ArticleURL] = []
        found_any = False
        with httpx.Client(
            headers=_HEADERS, timeout=self.timeout, follow_redirects=True
        ) as client:
            for candidate in sitemap_candidates:
                if candidate in self._visited:
                    continue
                try:
                    urls = self._process_sitemap(
                        client, candidate, domain, since, depth=0
                    )
                    if urls or candidate.endswith("sitemap.xml"):
                        found_any = True
                    all_urls.extend(urls)
                except httpx.HTTPStatusError as e:
                    logger.debug(
                        "sitemap_extractor: {url} → HTTP {status}",
                        url=candidate,
                        status=e.response.status_code,
                    )
                except Exception as e:
                    logger.debug(
                        "sitemap_extractor: {url} inaccessible — {err}",
                        url=candidate,
                        err=str(e),
                    )

        if not found_any:
            logger.warning(
                "sitemap_extractor: no sitemap found for {domain}",
                domain=domain,
            )

        # Deduplicate and sort newest-first
        seen: set[str] = set()
        unique: list[ArticleURL] = []
        for a in all_urls:
            if a.url not in seen:
                seen.add(a.url)
                unique.append(a)

        unique.sort(key=lambda a: a.lastmod or date.min, reverse=True)
        logger.info(
            "sitemap_extractor: {domain} → {count} article URLs since {since}",
            domain=domain,
            count=len(unique),
            since=since.isoformat(),
        )
        return unique

    def _sitemaps_from_robots(self, base: str) -> list[str]:
        """Parse robots.txt for Sitemap: directives."""
        urls: list[str] = []
        try:
            with httpx.Client(
                headers=_HEADERS, timeout=self.timeout, follow_redirects=True
            ) as client:
                resp = client.get(f"{base}/robots.txt")
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    stripped = line.strip()
                    if stripped.lower().startswith("sitemap:"):
                        _, _, loc = stripped.partition(":")
                        loc = loc.strip()
                        if loc.startswith("//"):
                            loc = "https:" + loc
                        if loc:
                            urls.append(loc)
        except Exception as exc:
            logger.debug(
                "sitemap_extractor: robots.txt parse failed for {base}: {err}",
                base=base,
                err=str(exc),
            )
        return urls

    def _process_sitemap(
        self,
        client: httpx.Client,
        sitemap_url: str,
        domain: str,
        since: date,
        depth: int,
    ) -> list[ArticleURL]:
        if depth > self.max_depth or sitemap_url in self._visited:
            return []
        self._visited.add(sitemap_url)

        for attempt in range(1, 3):
            try:
                resp = client.get(sitemap_url)
                resp.raise_for_status()
                break
            except httpx.TimeoutException:
                if attempt == 2:
                    logger.warning(
                        "sitemap_extractor: timeout fetching {url}",
                        url=sitemap_url,
                    )
                    return []
        else:
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            logger.debug(
                "sitemap_extractor: XML parse error at {url}: {err}",
                url=sitemap_url,
                err=str(e),
            )
            return []

        tag = root.tag
        if tag == f"{{{_SITEMAP_NS['sm']}}}sitemapindex":
            return self._parse_index(client, root, domain, since, depth)
        else:
            return self._parse_urlset(root, domain, since, sitemap_url)

    def _parse_index(
        self,
        client: httpx.Client,
        root: ET.Element,
        domain: str,
        since: date,
        depth: int,
    ) -> list[ArticleURL]:
        urls: list[ArticleURL] = []
        for sm_elem in root.findall("sm:sitemap", _SITEMAP_NS):
            loc = sm_elem.find("sm:loc", _SITEMAP_NS)
            if loc is None or not loc.text:
                continue
            child_url = loc.text.strip()

            # Skip child sitemaps whose lastmod is before `since`
            lm_elem = sm_elem.find("sm:lastmod", _SITEMAP_NS)
            if lm_elem is not None and lm_elem.text:
                lm = _parse_lastmod(lm_elem.text)
                if lm is not None and lm < since:
                    continue

            try:
                child_urls = self._process_sitemap(
                    client, child_url, domain, since, depth + 1
                )
                urls.extend(child_urls)
            except Exception as e:
                logger.debug(
                    "sitemap_extractor: child sitemap {url} error: {err}",
                    url=child_url,
                    err=str(e),
                )
        return urls

    def _parse_urlset(
        self,
        root: ET.Element,
        domain: str,
        since: date,
        source_sitemap: str,
    ) -> list[ArticleURL]:
        urls: list[ArticleURL] = []
        for url_elem in root.findall("sm:url", _SITEMAP_NS):
            loc = url_elem.find("sm:loc", _SITEMAP_NS)
            if loc is None or not loc.text:
                continue
            raw_url = loc.text.strip()

            # Filter by lastmod if present
            lm_elem = url_elem.find("sm:lastmod", _SITEMAP_NS)
            lastmod: date | None = None
            if lm_elem is not None and lm_elem.text:
                lastmod = _parse_lastmod(lm_elem.text)
                if lastmod is not None and lastmod < since:
                    continue

            if not _is_article_url(raw_url):
                continue

            urls.append(
                ArticleURL(
                    url=raw_url,
                    domain=domain,
                    lastmod=lastmod,
                    source_sitemap=source_sitemap,
                )
            )
        return urls
