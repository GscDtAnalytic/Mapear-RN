"""Regression tests for the 4 RN domains with selector_missing history.

Each test loads a saved HTML fixture (minimal excerpt of the real page),
runs it through ArticleParser, and asserts that title + body + date are
extracted. Fixtures are stored alongside this file in fixtures/.

Fixture provenance: fetched 2026-04-24 via HttpxScraper (Firefox UA,
no brotli encoding). Pages are trimmed to the relevant content section
+ head metadata to stay well under 200 KB.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mapear_rss.extraction.article_parser import ArticleParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"

_MIN_CONTENT_CHARS = 300


@pytest.fixture(scope="module")
def parser() -> ArticleParser:
    return ArticleParser()


def _load(name: str) -> str:
    return (FIXTURE_DIR / f"{name}_article.html").read_text(encoding="utf-8")


class TestNovoNoticias:
    URL = "https://www.novonoticias.com.br/com-uma-duvida-uniao-finaliza-preparacao-para-o-duelo-contra-o-ipojuca-pe/"

    def test_extracts_title(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("novonoticias"), self.URL, "novonoticias")
        assert article is not None
        assert "União" in article.title or "união" in article.title.lower()

    def test_extracts_body(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("novonoticias"), self.URL, "novonoticias")
        assert article is not None
        assert len(article.content) >= _MIN_CONTENT_CHARS

    def test_extracts_date(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("novonoticias"), self.URL, "novonoticias")
        assert article is not None
        assert article.published_at is not None
        assert article.published_at.year == 2026


class TestBlogdoBg:
    URL = "https://www.blogdobg.com.br/mpox-no-rn-chega-a-12-casos-e-novos-registros-sao-confirmados-pela-saude/"

    def test_extracts_title(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("blogdobg"), self.URL, "blogdobg")
        assert article is not None
        assert "Mpox" in article.title or "mpox" in article.title.lower()

    def test_extracts_body(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("blogdobg"), self.URL, "blogdobg")
        assert article is not None
        assert len(article.content) >= _MIN_CONTENT_CHARS

    def test_extracts_date(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("blogdobg"), self.URL, "blogdobg")
        assert article is not None
        assert article.published_at is not None
        assert article.published_at.year == 2026


class TestAgorarn:
    URL = "https://agorarn.com.br/ultimas/ministerio-publico-prefeito-tiktoker-abriu-buraco/"

    def test_extracts_title(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("agorarn"), self.URL, "agorarn")
        assert article is not None
        assert len(article.title) > 10

    def test_extracts_body(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("agorarn"), self.URL, "agorarn")
        assert article is not None
        assert len(article.content) >= _MIN_CONTENT_CHARS

    def test_domain_selector_fires_as_safety_net(self, parser: ArticleParser) -> None:
        # Agorarn exposes NewsArticle JSON-LD *without* articleBody.
        # The domain selector (div.contentnotice) is the primary safety net
        # when trafilatura also misses. Verify the fixture content is enough
        # for at least one path to succeed.
        html = _load("agorarn")
        assert "contentnotice" in html, "fixture must contain the domain selector"
        article = parser.parse(html, self.URL, "agorarn")
        assert article is not None

    def test_extracts_date(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("agorarn"), self.URL, "agorarn")
        assert article is not None
        assert article.published_at is not None
        assert article.published_at.year == 2026


class TestTribunaDoNorte:
    URL = "https://tribunadonorte.com.br/rio-grande-do-norte/nadia-belarmino-e-a-primeira-diretora-presidente-efetiva-nos-56-anos-da-caern/"

    def test_extracts_title(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("tribunadonorte"), self.URL, "tribunadonorte")
        assert article is not None
        assert "Nádia" in article.title or "Caern" in article.title

    def test_extracts_body(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("tribunadonorte"), self.URL, "tribunadonorte")
        assert article is not None
        assert len(article.content) >= _MIN_CONTENT_CHARS

    def test_extracts_date(self, parser: ArticleParser) -> None:
        article = parser.parse(_load("tribunadonorte"), self.URL, "tribunadonorte")
        assert article is not None
        assert article.published_at is not None
        assert article.published_at.year == 2026
