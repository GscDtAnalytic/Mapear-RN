"""Tests for sentiment analysis (skip mode)."""

import pytest

from mapear_domain.models.base import SilverArticle
from mapear_nlp.sentiment import SentimentAnalyzer


@pytest.fixture
def analyzer(monkeypatch: pytest.MonkeyPatch) -> SentimentAnalyzer:
    monkeypatch.setenv("ENRICHMENT_MODE", "skip")
    return SentimentAnalyzer()


@pytest.fixture
def silver_article() -> SilverArticle:
    return SilverArticle(
        url="https://example.com/noticia",
        source_feed="test",
        title="Prefeito de Natal anuncia obra em hospital",
        content_clean=(
            "O prefeito Paulinho Freire anunciou investimentos "
            "de R$ 10 milhões em saúde pública em Natal."
        ),
        extracted_at="2026-04-03T12:00:00",
        content_hash="abc123",
        mentioned_cities=["Natal"],
        mentioned_mayors=["Paulinho Freire"],
        mentioned_parties=["União Brasil"],
        is_rn_relevant=True,
    )


class TestSentimentAnalyzer:
    def test_skip_mode_returns_neutral(
        self, analyzer: SentimentAnalyzer, silver_article: SilverArticle
    ) -> None:
        result = analyzer.analyze_article(silver_article)
        assert result["sentiment_overall"] == 0.0
        assert result["sentiment_by_entity"] == []

    def test_batch_returns_correct_count(
        self, analyzer: SentimentAnalyzer, silver_article: SilverArticle
    ) -> None:
        results = analyzer.analyze_batch([silver_article, silver_article])
        assert len(results) == 2

    def test_normalize_star_score(self) -> None:
        score = SentimentAnalyzer._normalize_score({"label": "5 stars", "score": 0.95})
        assert score == 1.0

        score = SentimentAnalyzer._normalize_score({"label": "1 star", "score": 0.9})
        assert score == -1.0

        score = SentimentAnalyzer._normalize_score({"label": "3 stars", "score": 0.8})
        assert score == 0.0

    def test_split_sentences(self) -> None:
        text = (
            "Primeira frase com conteúdo suficiente. "
            "Segunda frase também com conteúdo. "
            "Terceira frase aqui no final da matéria."
        )
        sentences = SentimentAnalyzer._split_sentences(text)
        assert len(sentences) == 3
