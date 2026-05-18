"""Tests for RSS feed reader."""

from unittest.mock import MagicMock, patch

import pytest

from mapear_rss.discovery.rss_reader import RSSReader

SAMPLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Diário Fake</title>
    <item>
      <title>Prefeito de Testópolis anuncia obra</title>
      <link>https://diariofake.com.br/noticia-1</link>
      <pubDate>Tue, 01 Apr 2026 10:00:00 -0300</pubDate>
    </item>
    <item>
      <title>Vilafake recebe investimento</title>
      <link>https://diariofake.com.br/noticia-2</link>
    </item>
    <item>
      <description>Entry without link</description>
    </item>
  </channel>
</rss>
"""

# General-interest feed XML: mix of region and non-region entries. Used to
# exercise the BL-08 discovery filter that drops non-region entries.
MIXED_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Portal Nacional</title>
    <item>
      <title>Trânsito em São Paulo</title>
      <description>Avenidas paulistanas com lentidão</description>
      <link>https://folha.com/sp-1</link>
    </item>
    <item>
      <title>Governador Teste visita Testópolis</title>
      <description>Anúncio de investimento regional</description>
      <link>https://folha.com/rn-1</link>
    </item>
    <item>
      <title>Inflação nacional</title>
      <description>IPCA de março</description>
      <link>https://folha.com/eco-1</link>
    </item>
  </channel>
</rss>
"""


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> RSSReader:
    monkeypatch.setenv("ENRICHMENT_MODE", "skip")
    return RSSReader()


class TestRSSReader:
    def test_fetch_feed_parses_entries(
        self, reader: RSSReader, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = SAMPLE_RSS_XML
        mock_response.raise_for_status = MagicMock()

        with patch(
            "mapear_rss.discovery.rss_reader.httpx.get", return_value=mock_response
        ):
            urls = reader.fetch_feed("https://tribunadonorte.com.br/feed/")

        # 2 entries with links, 1 without
        assert len(urls) == 2
        assert str(urls[0].url) == "https://diariofake.com.br/noticia-1"
        assert urls[0].title == "Prefeito de Testópolis anuncia obra"
        assert urls[0].source_feed == "https://tribunadonorte.com.br/feed/"

    def test_fetch_feed_skips_entries_without_link(self, reader: RSSReader) -> None:
        mock_response = MagicMock()
        mock_response.text = SAMPLE_RSS_XML
        mock_response.raise_for_status = MagicMock()

        with patch(
            "mapear_rss.discovery.rss_reader.httpx.get", return_value=mock_response
        ):
            urls = reader.fetch_feed("https://example.com/feed")

        # Entry sem link é ignorado
        links = [str(u.url) for u in urls]
        assert all("example.com" not in link or "noticia" in link for link in links)

    def test_fetch_feed_empty_rss(self, reader: RSSReader) -> None:
        empty_rss = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        mock_response = MagicMock()
        mock_response.text = empty_rss
        mock_response.raise_for_status = MagicMock()

        with patch(
            "mapear_rss.discovery.rss_reader.httpx.get", return_value=mock_response
        ):
            urls = reader.fetch_feed("https://example.com/feed")

        assert urls == []

    def test_fetch_multiple_aggregates(self, reader: RSSReader) -> None:
        mock_response = MagicMock()
        mock_response.text = SAMPLE_RSS_XML
        mock_response.raise_for_status = MagicMock()

        with patch(
            "mapear_rss.discovery.rss_reader.httpx.get", return_value=mock_response
        ):
            urls = reader.fetch_multiple(
                [
                    "https://feed1.com/rss",
                    "https://feed2.com/rss",
                ]
            )

        assert len(urls) == 4  # 2 entries per feed × 2 feeds

    def test_fetch_multiple_continues_on_failure(self, reader: RSSReader) -> None:
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:  # retry_on_network_error has max_attempts=3
                raise ConnectionError("fail")
            mock = MagicMock()
            mock.text = SAMPLE_RSS_XML
            mock.raise_for_status = MagicMock()
            return mock

        with patch(
            "mapear_rss.discovery.rss_reader.httpx.get", side_effect=side_effect
        ):
            urls = reader.fetch_multiple(
                [
                    "https://failing.com/feed",
                    "https://working.com/feed",
                ]
            )

        # First feed fails (after retries), second succeeds
        assert len(urls) == 2

    def test_parse_date_valid(self) -> None:
        entry = {"published_parsed": (2026, 4, 1, 10, 0, 0, 1, 91, 0)}
        dt = RSSReader._parse_date(entry)
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 4

    def test_parse_date_none(self) -> None:
        assert RSSReader._parse_date({}) is None

    def test_parse_date_invalid(self) -> None:
        assert RSSReader._parse_date({"published_parsed": None}) is None

    def test_fetch_feed_filters_non_rn_entries(self, reader: RSSReader) -> None:
        mock_response = MagicMock()
        mock_response.text = MIXED_RSS_XML
        mock_response.raise_for_status = MagicMock()

        with patch(
            "mapear_rss.discovery.rss_reader.httpx.get", return_value=mock_response
        ):
            urls = reader.fetch_feed("https://folha.com/feed")

        # Only the "Governador Teste visita Testópolis" entry survives the filter.
        assert len(urls) == 1
        assert str(urls[0].url) == "https://folha.com/rn-1"

    def test_fetch_feed_rn_focused_bypasses_filter(self, reader: RSSReader) -> None:
        mock_response = MagicMock()
        mock_response.text = MIXED_RSS_XML
        mock_response.raise_for_status = MagicMock()

        with patch(
            "mapear_rss.discovery.rss_reader.httpx.get", return_value=mock_response
        ):
            urls = reader.fetch_feed("https://folha.com/feed", rn_focused=True)

        # All 3 entries preserved — focused feeds skip the RN filter.
        assert len(urls) == 3

    def test_fetch_multiple_marks_rn_focused_feeds(self, reader: RSSReader) -> None:
        mock_response = MagicMock()
        mock_response.text = MIXED_RSS_XML
        mock_response.raise_for_status = MagicMock()

        focused = "https://tribunadonorte.com.br/feed/"
        general = "https://folha.com/feed"

        with patch(
            "mapear_rss.discovery.rss_reader.httpx.get", return_value=mock_response
        ):
            urls = reader.fetch_multiple([focused, general], rn_focused_feeds={focused})

        # Focused → all 3 entries; general → only 1 RN entry. Total: 4.
        assert len(urls) == 4
