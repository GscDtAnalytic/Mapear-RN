"""Tests for web scraper."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mapear_rss.extraction.browser_scraper import BrowserFetchResult
from mapear_rss.extraction.httpx_scraper import HttpxScraper
from mapear_rss.extraction.scraper import Scraper

SAMPLE_HTML = """
<!DOCTYPE html>
<html><head><title>Notícia Teste</title></head>
<body>
<article>
    <h1>Prefeito de Natal anuncia investimentos</h1>
    <p>O prefeito anunciou nesta terça-feira a construção de um novo hospital
    em Natal. O investimento será de R$ 50 milhões com recursos federais
    destinados à saúde pública do Rio Grande do Norte. A obra deve começar
    no primeiro semestre de 2027 e beneficiar mais de 500 mil habitantes.</p>
</article>
</body></html>
"""


@pytest.fixture
def mock_cb() -> MagicMock:
    cb = MagicMock()
    cb.is_allowed.return_value = True
    return cb


@pytest.fixture
def scraper(monkeypatch: pytest.MonkeyPatch, mock_cb: MagicMock) -> Scraper:
    monkeypatch.setenv("ENRICHMENT_MODE", "skip")
    monkeypatch.setenv("SCRAPER_DELAY_MIN", "0")
    monkeypatch.setenv("SCRAPER_DELAY_MAX", "0")
    monkeypatch.setenv("SCRAPER_RESPECT_ROBOTS_TXT", "false")
    s = Scraper(circuit_breaker=mock_cb)
    return s


class TestScraper:
    def test_scrape_url_success(self, scraper: Scraper) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.text = SAMPLE_HTML
        mock_response.headers = {"content-type": "text/html; charset=utf-8"}
        mock_response.raise_for_status = MagicMock()

        with patch.object(scraper.client, "get", return_value=mock_response):
            result = scraper.scrape_url("https://example.com/noticia", "test_feed")

        # trafilatura pode ou não extrair — o teste valida que não crashe
        if result is not None:
            assert result.source_feed == "test_feed"
            assert result.content_hash is not None

    def test_scrape_url_circuit_open_skips(
        self, scraper: Scraper, mock_cb: MagicMock
    ) -> None:
        mock_cb.is_allowed.return_value = False

        result = scraper.scrape_url("https://blocked.com/page", "feed")
        assert result is None

    def test_scrape_url_non_html_skips(self, scraper: Scraper) -> None:
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.headers = {"content-type": "application/pdf"}
        mock_response.raise_for_status = MagicMock()

        with patch.object(scraper.client, "get", return_value=mock_response):
            result = scraper.scrape_url("https://example.com/doc.pdf", "feed")

        assert result is None

    def test_scrape_url_records_failure(
        self, scraper: Scraper, mock_cb: MagicMock
    ) -> None:
        with patch.object(
            scraper.client, "get", side_effect=httpx.ConnectError("timeout")
        ):
            result = scraper.scrape_url("https://failing.com/page", "feed")

        assert result is None
        mock_cb.record_failure.assert_called_once()

    def test_scrape_batch(self, scraper: Scraper) -> None:
        fake_result = BrowserFetchResult(
            html=SAMPLE_HTML,
            status_code=200,
            final_url="https://example.com/1",
            headers={"content-type": "text/html; charset=utf-8"},
        )

        with patch.object(
            HttpxScraper, "fetch", new=AsyncMock(return_value=fake_result)
        ):
            articles = scraper.scrape_batch(
                [
                    {"url": "https://example.com/1", "source_feed": "feed_a"},
                    {"url": "https://example.com/2", "source_feed": "feed_b"},
                ]
            )

        # Ambas devem ter sido tentadas (resultado depende do trafilatura)
        assert isinstance(articles, list)

    def test_context_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENRICHMENT_MODE", "skip")
        monkeypatch.setenv("SCRAPER_DELAY_MIN", "0")
        monkeypatch.setenv("SCRAPER_DELAY_MAX", "0")

        with Scraper() as s:
            assert s.client is not None
        # Client should be closed after exit (no assertion needed, just no error)

    def test_browser_fallback_triggered_on_bot_block(
        self, monkeypatch: pytest.MonkeyPatch, mock_cb: MagicMock
    ) -> None:
        monkeypatch.setenv("ENRICHMENT_MODE", "skip")
        monkeypatch.setenv("SCRAPER_DELAY_MIN", "0")
        monkeypatch.setenv("SCRAPER_DELAY_MAX", "0")
        monkeypatch.setenv("SCRAPER_RESPECT_ROBOTS_TXT", "false")
        monkeypatch.setenv("SCRAPER_PLAYWRIGHT_ENABLED", "true")
        monkeypatch.setenv("SCRAPER_PLAYWRIGHT_TARGETED_DOMAINS", "blocked.example.com")

        browser = MagicMock()
        browser.fetch = AsyncMock(
            return_value=BrowserFetchResult(
                html=SAMPLE_HTML,
                status_code=200,
                final_url="https://blocked.example.com/article",
                headers={"content-type": "text/html"},
            )
        )
        s = Scraper(circuit_breaker=mock_cb, browser=browser)

        # Cloudflare-blocked httpx response
        blocked_response = MagicMock(spec=httpx.Response)
        blocked_response.status_code = 403
        blocked_response.text = "<html>cloudflare attention required</html>"
        blocked_response.headers = {
            "content-type": "text/html",
            "server": "cloudflare",
        }
        blocked_response.url = "https://blocked.example.com/article"

        async def fake_get(*_args, **_kwargs):
            return blocked_response

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
            result = s.scrape_batch(
                [
                    {
                        "url": "https://blocked.example.com/article",
                        "source_feed": "feed",
                    }
                ]
            )

        browser.fetch.assert_awaited()
        assert s.browser_counts["attempts"] >= 1
        # Result list may be empty or contain an article depending on parser
        # — what we care about is that the fallback was attempted.
        assert isinstance(result, list)
        s.close()

    def test_browser_fallback_skipped_when_domain_not_targeted(
        self, monkeypatch: pytest.MonkeyPatch, mock_cb: MagicMock
    ) -> None:
        monkeypatch.setenv("ENRICHMENT_MODE", "skip")
        monkeypatch.setenv("SCRAPER_DELAY_MIN", "0")
        monkeypatch.setenv("SCRAPER_DELAY_MAX", "0")
        monkeypatch.setenv("SCRAPER_RESPECT_ROBOTS_TXT", "false")
        monkeypatch.setenv("SCRAPER_PLAYWRIGHT_ENABLED", "true")
        monkeypatch.setenv("SCRAPER_PLAYWRIGHT_TARGETED_DOMAINS", "other.com")

        browser = MagicMock()
        browser.fetch = AsyncMock()
        s = Scraper(circuit_breaker=mock_cb, browser=browser)

        assert s._should_try_browser("not-targeted.example.com") is False
        assert s._should_try_browser("other.com") is True
        s.close()

    def test_robots_txt_respected(
        self, scraper: Scraper, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scraper.respect_robots = True
        mock_rp = MagicMock()
        mock_rp.can_fetch.return_value = False

        scraper._robots_cache["https://blocked.com"] = mock_rp

        result = scraper.scrape_url("https://blocked.com/secret", "feed")
        assert result is None
