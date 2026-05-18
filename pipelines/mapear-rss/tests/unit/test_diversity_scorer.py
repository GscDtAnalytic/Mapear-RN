"""Tests for source diversity scoring."""

import json
from datetime import UTC, datetime

import pytest

from mapear_domain.models.base import RawArticle
from mapear_rss.analysis.diversity_scorer import (
    DEFAULT_CONCENTRATION_THRESHOLD,
    DiversityScorer,
)


def _article(source_feed: str, idx: int = 0) -> RawArticle:
    return RawArticle(
        url=f"https://example.com/article-{source_feed.replace('/', '-')}-{idx}",
        source_feed=source_feed,
        title=f"Article {idx}",
        content="content body",
        extracted_at=datetime.now(UTC),
        content_hash=f"hash-{source_feed}-{idx}",
    )


class TestDiversityScorerEmpty:
    def test_empty_batch_returns_zero_metrics(self) -> None:
        report = DiversityScorer().compute([])
        assert report.total_articles == 0
        assert report.unique_sources == 0
        assert report.source_concentration_index == 0.0
        assert report.dominant_source is None
        assert not report.concentration_alert

    def test_empty_batch_threshold_preserved(self) -> None:
        scorer = DiversityScorer(threshold=0.5)
        report = scorer.compute([])
        assert report.threshold == 0.5


class TestDiversityScorerSingleSource:
    def test_single_source_monopoly(self) -> None:
        articles = [_article("feed-a", i) for i in range(10)]
        report = DiversityScorer(threshold=0.70).compute(articles)

        assert report.source_concentration_index == pytest.approx(1.0, abs=0.001)
        assert report.dominant_source == "feed-a"
        assert report.dominant_source_share == pytest.approx(1.0, abs=0.001)
        assert report.concentration_alert

    def test_single_source_total_correct(self) -> None:
        articles = [_article("feed-a", i) for i in range(5)]
        report = DiversityScorer().compute(articles)
        assert report.total_articles == 5
        assert report.unique_sources == 1


class TestDiversityScorerMultipleSources:
    def test_balanced_two_sources_hhi(self) -> None:
        articles = [_article("feed-a", i) for i in range(5)] + [
            _article("feed-b", i) for i in range(5)
        ]
        report = DiversityScorer().compute(articles)
        assert report.source_concentration_index == pytest.approx(0.5, abs=0.001)
        assert report.dominant_source_share == pytest.approx(0.5, abs=0.001)
        assert not report.concentration_alert

    def test_hhi_three_equal_sources_is_one_third(self) -> None:
        articles = (
            [_article("a", i) for i in range(4)]
            + [_article("b", i) for i in range(4)]
            + [_article("c", i) for i in range(4)]
        )
        report = DiversityScorer().compute(articles)
        assert report.source_concentration_index == pytest.approx(1 / 3, abs=0.001)
        assert report.unique_sources == 3
        assert report.total_articles == 12

    def test_concentration_alert_above_threshold(self) -> None:
        # 8 from feed-a (80%), 2 from feed-b (20%)
        articles = [_article("feed-a", i) for i in range(8)] + [
            _article("feed-b", i) for i in range(2)
        ]
        report = DiversityScorer(threshold=0.70).compute(articles)
        assert report.dominant_source_share == pytest.approx(0.8, abs=0.001)
        assert report.concentration_alert

    def test_no_alert_below_threshold(self) -> None:
        # 6 from feed-a (60%), 4 from feed-b (40%)
        articles = [_article("feed-a", i) for i in range(6)] + [
            _article("feed-b", i) for i in range(4)
        ]
        report = DiversityScorer(threshold=0.70).compute(articles)
        assert report.dominant_source_share == pytest.approx(0.6, abs=0.001)
        assert not report.concentration_alert

    def test_source_distribution_sorted_descending(self) -> None:
        articles = (
            [_article("feed-c", i) for i in range(3)]
            + [_article("feed-a", i) for i in range(7)]
            + [_article("feed-b", i) for i in range(2)]
        )
        report = DiversityScorer().compute(articles)
        sources = list(report.source_distribution.keys())
        assert sources[0] == "feed-a"
        assert sources[-1] == "feed-b"

    def test_custom_threshold(self) -> None:
        # 55% from feed-a — below 0.70 but above 0.50
        articles = [_article("feed-a", i) for i in range(11)] + [
            _article("feed-b", i) for i in range(9)
        ]
        assert not DiversityScorer(threshold=0.70).compute(articles).concentration_alert
        assert DiversityScorer(threshold=0.50).compute(articles).concentration_alert


class TestDiversityScorerToDict:
    def test_to_dict_is_json_serializable(self) -> None:
        articles = [_article("feed-a", i) for i in range(3)]
        scorer = DiversityScorer()
        report = scorer.compute(articles)
        d = scorer.to_dict(report)
        json.dumps(d)  # must not raise

    def test_to_dict_has_required_keys(self) -> None:
        scorer = DiversityScorer()
        report = scorer.compute([_article("x")])
        d = scorer.to_dict(report)
        for key in (
            "total_articles",
            "unique_sources",
            "source_distribution",
            "source_concentration_index",
            "dominant_source",
            "dominant_source_share",
            "concentration_alert",
            "threshold",
        ):
            assert key in d, f"Missing key: {key}"

    def test_default_threshold_value(self) -> None:
        assert DEFAULT_CONCENTRATION_THRESHOLD == 0.70
