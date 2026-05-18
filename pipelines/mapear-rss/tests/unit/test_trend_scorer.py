"""Tests for trend scoring."""

from datetime import UTC, datetime, timedelta

from mapear_domain.models.base import SilverArticle
from mapear_nlp.trend_scorer import TrendScorer


def _make_silver(title: str, content: str, hours_ago: float) -> SilverArticle:
    pub = datetime.now(UTC) - timedelta(hours=hours_ago)
    return SilverArticle(
        url=f"https://example.com/{hash(title)}",
        source_feed="test",
        title=title,
        content_clean=content,
        extracted_at=datetime.now(UTC).isoformat(),
        content_hash=f"hash_{hash(title)}",
        published_at=pub,
        is_rn_relevant=True,
    )


class TestTrendScorer:
    def test_no_mentions_returns_zero(self) -> None:
        scorer = TrendScorer()
        articles = [_make_silver("Outro assunto", "Nada relevante aqui", 1)]
        score = scorer.score_entity("Natal", articles)
        assert score == 0.0

    def test_recent_mentions_score_higher(self) -> None:
        scorer = TrendScorer(window_hours=48)
        recent = [
            _make_silver("Natal recebe obras", "Investimentos em Natal", 1),
            _make_silver("Natal avança", "Natal cresce em infraestrutura", 2),
        ]
        old = [
            _make_silver("Natal antiga", "Natal há muito tempo", 47),
        ]
        score_recent = scorer.score_entity("Natal", recent)
        score_old = scorer.score_entity("Natal", old)
        assert score_recent > score_old

    def test_more_mentions_score_higher(self) -> None:
        scorer = TrendScorer()
        few = [_make_silver("Natal 1", "Natal notícia", 5)]
        many = [_make_silver(f"Natal {i}", f"Natal notícia {i}", 5) for i in range(10)]
        score_few = scorer.score_entity("Natal", few)
        score_many = scorer.score_entity("Natal", many)
        assert score_many > score_few

    def test_score_batch(self) -> None:
        scorer = TrendScorer()
        articles = [
            _make_silver("Natal obras", "Natal investimentos", 2),
            _make_silver("Mossoró escola", "Mossoró educação", 3),
        ]
        scores = scorer.score_batch(["Natal", "Mossoró", "Caicó"], articles)
        assert "Natal" in scores
        assert "Mossoró" in scores
        assert scores["Caicó"] == 0.0

    def test_score_range(self) -> None:
        scorer = TrendScorer()
        articles = [
            _make_silver(f"Natal {i}", f"Natal notícia {i}", i * 0.5) for i in range(20)
        ]
        score = scorer.score_entity("Natal", articles)
        assert 0.0 <= score <= 1.0
