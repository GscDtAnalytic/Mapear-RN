"""Unit tests for SitemapExtractor.

Integration tests marked with @pytest.mark.integration require internet
access and are skipped in normal CI runs.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from mapear_rss.discovery.sitemap_extractor import (
    ArticleURL,
    SitemapExtractor,
    _is_article_url,
    _parse_lastmod,
)

# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

_SIMPLE_SITEMAP = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/2025/03/politica-rn-prefeito</loc>
    <lastmod>2025-03-15</lastmod>
  </url>
  <url>
    <loc>https://example.com/2025/01/vereadores-natal</loc>
    <lastmod>2025-01-10</lastmod>
  </url>
  <url>
    <loc>https://example.com/2024/12/noticia-antiga</loc>
    <lastmod>2024-12-20</lastmod>
  </url>
</urlset>
"""

_INDEX_SITEMAP = """\
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://example.com/sitemap-2025.xml</loc>
    <lastmod>2025-03-15</lastmod>
  </sitemap>
  <sitemap>
    <loc>https://example.com/sitemap-2024.xml</loc>
    <lastmod>2024-12-31</lastmod>
  </sitemap>
</sitemapindex>
"""

_CHILD_SITEMAP = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/2025/02/eleicoes-rn</loc>
    <lastmod>2025-02-01</lastmod>
  </url>
</urlset>
"""

_EMPTY_URLSET = (
    "<?xml version='1.0'?>"
    "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'></urlset>"
)
_EMPTY_INDEX = (
    "<?xml version='1.0'?>"
    "<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
    "</sitemapindex>"
)


# ---------------------------------------------------------------------------
# Unit tests — _is_article_url heuristic
# ---------------------------------------------------------------------------


class TestIsArticleUrl:
    def test_date_path_accepted(self) -> None:
        assert _is_article_url("https://example.com/2025/03/noticia-rn")

    def test_three_segments_accepted(self) -> None:
        assert _is_article_url("https://example.com/politica/rn/vereadores-natal")

    def test_long_slug_accepted(self) -> None:
        assert _is_article_url(
            "https://example.com/prefeito-de-natal-anuncia-novo-projeto-de-lei"
        )

    def test_categoria_rejected(self) -> None:
        assert not _is_article_url("https://example.com/categoria/politica")

    def test_tag_rejected(self) -> None:
        assert not _is_article_url("https://example.com/tag/rn")

    def test_autor_rejected(self) -> None:
        assert not _is_article_url("https://example.com/autor/joao-silva")

    def test_page_rejected(self) -> None:
        assert not _is_article_url("https://example.com/page/2")

    def test_query_string_rejected(self) -> None:
        assert not _is_article_url("https://example.com/noticia?id=123")

    def test_feed_rejected(self) -> None:
        assert not _is_article_url("https://example.com/feed/")


# ---------------------------------------------------------------------------
# Unit tests — _parse_lastmod
# ---------------------------------------------------------------------------


class TestParseLastmod:
    def test_full_iso_with_tz(self) -> None:
        result = _parse_lastmod("2025-03-15T10:30:00+00:00")
        assert result == date(2025, 3, 15)

    def test_date_only(self) -> None:
        result = _parse_lastmod("2025-01-10")
        assert result == date(2025, 1, 10)

    def test_z_suffix(self) -> None:
        result = _parse_lastmod("2025-06-01T00:00:00Z")
        assert result == date(2025, 6, 1)

    def test_invalid_returns_none(self) -> None:
        assert _parse_lastmod("not-a-date") is None
        assert _parse_lastmod("") is None


# ---------------------------------------------------------------------------
# Unit tests — SitemapExtractor with mocked HTTP
# ---------------------------------------------------------------------------

_HTTPX_CLIENT = "mapear_rss.discovery.sitemap_extractor.httpx.Client"


def _mock_response(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


class TestSitemapExtractorSimple:
    def _patched_client(self, url_responses: dict[str, str]):
        """Return a context manager that simulates httpx.Client.get(url)."""
        client_mock = MagicMock()

        def _get(url, **_kwargs):
            if url in url_responses:
                return _mock_response(url_responses[url])
            resp = MagicMock()
            resp.status_code = 404
            resp.raise_for_status = MagicMock(side_effect=Exception(f"404 {url}"))
            return resp

        client_mock.get = _get
        client_mock.__enter__ = MagicMock(return_value=client_mock)
        client_mock.__exit__ = MagicMock(return_value=False)
        return client_mock

    def test_simple_sitemap_date_filter(self) -> None:
        since = date(2025, 1, 1)
        extractor = SitemapExtractor()

        url_map = {
            "https://example.com/sitemap.xml": _SIMPLE_SITEMAP,
            "https://example.com/robots.txt": "",
        }

        with patch(_HTTPX_CLIENT) as mock_client:
            mock_client.return_value = self._patched_client(url_map)
            urls = extractor.extract_from_domain("example.com", since)

        # 2024-12-20 article should be filtered out; 2025-03 and 2025-01 kept
        url_strs = [a.url for a in urls]
        assert "https://example.com/2025/03/politica-rn-prefeito" in url_strs
        assert "https://example.com/2025/01/vereadores-natal" in url_strs
        assert "https://example.com/2024/12/noticia-antiga" not in url_strs

    def test_simple_sitemap_sorted_newest_first(self) -> None:
        since = date(2025, 1, 1)
        extractor = SitemapExtractor()

        url_map = {
            "https://example.com/sitemap.xml": _SIMPLE_SITEMAP,
            "https://example.com/robots.txt": "",
        }

        with patch(_HTTPX_CLIENT) as mock_client:
            mock_client.return_value = self._patched_client(url_map)
            urls = extractor.extract_from_domain("example.com", since)

        assert urls[0].lastmod >= urls[-1].lastmod  # type: ignore[operator]

    def test_indexed_sitemap_recurses(self) -> None:
        since = date(2025, 1, 1)
        extractor = SitemapExtractor()

        url_map = {
            "https://example.com/sitemap.xml": _INDEX_SITEMAP,
            "https://example.com/sitemap-2025.xml": _CHILD_SITEMAP,
            "https://example.com/sitemap-2024.xml": "",  # old, filtered by lastmod
            "https://example.com/sitemap_index.xml": "",
            "https://example.com/robots.txt": "",
        }

        with patch(_HTTPX_CLIENT) as mock_client:
            mock_client.return_value = self._patched_client(url_map)
            urls = extractor.extract_from_domain("example.com", since)

        url_strs = [a.url for a in urls]
        assert "https://example.com/2025/02/eleicoes-rn" in url_strs

    def test_robots_sitemap_discovery(self) -> None:
        since = date(2025, 1, 1)
        extractor = SitemapExtractor()

        robots = "User-agent: *\nDisallow: /admin\nSitemap: https://example.com/custom-sitemap.xml\n"

        url_map = {
            "https://example.com/sitemap.xml": _EMPTY_URLSET,
            "https://example.com/sitemap_index.xml": _EMPTY_INDEX,
            "https://example.com/robots.txt": robots,
            "https://example.com/custom-sitemap.xml": _SIMPLE_SITEMAP,
        }

        with patch(_HTTPX_CLIENT) as mock_client:
            mock_client.return_value = self._patched_client(url_map)
            urls = extractor.extract_from_domain("example.com", since)

        url_strs = [a.url for a in urls]
        assert any("politica-rn-prefeito" in u for u in url_strs)

    def test_to_discovered_url_conversion(self) -> None:
        article = ArticleURL(
            url="https://example.com/2025/01/noticia",
            domain="example.com",
            lastmod=date(2025, 1, 15),
            source_sitemap="https://example.com/sitemap.xml",
        )
        discovered = article.to_discovered()
        assert str(discovered.url) == "https://example.com/2025/01/noticia"
        assert discovered.published_at is not None
        assert discovered.published_at.year == 2025
        assert discovered.source_feed == "https://example.com/sitemap.xml"

    def test_no_sitemap_returns_empty(self) -> None:
        since = date(2025, 1, 1)
        extractor = SitemapExtractor()

        # All requests return empty/unparseable content
        url_map: dict[str, str] = {}

        with patch(_HTTPX_CLIENT) as mock_client:
            mock_client.return_value = self._patched_client(url_map)
            urls = extractor.extract_from_domain("nonexistent-domain.com", since)

        assert urls == []


# ---------------------------------------------------------------------------
# Integration tests — real network (skipped in CI)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_real_domain_returns_articles() -> None:
    """Fetch real sitemap from tribunadonorte.com.br and check results."""
    extractor = SitemapExtractor()
    since = date(2025, 1, 1)
    urls = extractor.extract_from_domain("tribunadonorte.com.br", since)
    assert len(urls) >= 10, f"Expected >=10 articles, got {len(urls)}"
    # All returned URLs should pass the article heuristic
    for a in urls[:20]:
        assert a.lastmod is None or a.lastmod >= since
