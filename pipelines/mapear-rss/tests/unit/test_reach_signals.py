"""Unit tests for compute_rss_reach_per_person."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mapear_domain.models.base import GoldArticle
from mapear_rss.reach_signals import compute_rss_reach_per_person


def _gold(
    *,
    person_id: str | None,
    published_at: datetime | None,
    url: str = "https://example.com/x",
    content_hash: str = "h",
) -> GoldArticle:
    """Minimal GoldArticle factory — only fields the reach calc reads."""
    return GoldArticle(
        url=url,
        source_feed="test",
        title="t",
        content_clean="c",
        content_hash=content_hash,
        is_rn_relevant=True,
        person_id=person_id,
        published_at=published_at,
        source_type="rss",
    )


def test_empty_input_returns_empty_map():
    assert compute_rss_reach_per_person([]) == {}


def test_articles_without_person_id_are_excluded():
    now = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    articles = [
        _gold(person_id=None, published_at=now, content_hash="a"),
        _gold(person_id=None, published_at=now, content_hash="b"),
    ]
    assert compute_rss_reach_per_person(articles) == {}


def test_single_article_yields_minimum_velocity():
    """One article cannot define velocity → floor at 0.1 by convention."""
    now = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    articles = [_gold(person_id="p1", published_at=now)]
    out = compute_rss_reach_per_person(articles)
    assert out["p1"] == (1, 0.1, 0)


def test_two_articles_same_instant_burst_velocity():
    now = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    articles = [
        _gold(person_id="p1", published_at=now, content_hash="a"),
        _gold(person_id="p1", published_at=now, content_hash="b"),
    ]
    volume, velocity, engagement = compute_rss_reach_per_person(articles)["p1"]
    assert (volume, engagement) == (2, 0)
    assert velocity == 0.5


def test_velocity_proportional_to_rate_per_hour():
    """4 articles spread over 4h → 1 art/h → 1/5 = 0.2."""
    base = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    articles = [
        _gold(
            person_id="p1",
            published_at=base + timedelta(hours=h),
            content_hash=f"h{h}",
        )
        for h in (0, 1, 2, 3)
    ]
    volume, velocity, _ = compute_rss_reach_per_person(articles)["p1"]
    assert volume == 4
    assert velocity == pytest.approx(4 / 3 / 5.0, rel=1e-3)


def test_velocity_clamped_at_1_for_high_rates():
    """50 articles in 1h → 50/h → would be 10.0, clamped to 1.0."""
    base = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    articles = [
        _gold(
            person_id="p1",
            published_at=base + timedelta(minutes=m),
            content_hash=f"h{m}",
        )
        for m in range(50)
    ]
    _, velocity, _ = compute_rss_reach_per_person(articles)["p1"]
    assert velocity == 1.0


def test_articles_grouped_per_person_independently():
    base = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    articles = [
        _gold(person_id="p1", published_at=base, content_hash="a"),
        _gold(person_id="p1", published_at=base + timedelta(hours=1), content_hash="b"),
        _gold(person_id="p2", published_at=base, content_hash="c"),
    ]
    out = compute_rss_reach_per_person(articles)
    assert out["p1"][0] == 2
    assert out["p2"][0] == 1
    assert out["p2"][1] == 0.1  # single article


def test_naive_datetime_is_normalized_to_utc():
    naive = datetime(2026, 5, 10, 12, 0)  # no tzinfo
    aware = datetime(2026, 5, 10, 13, 0, tzinfo=UTC)
    articles = [
        _gold(person_id="p1", published_at=naive, content_hash="a"),
        _gold(person_id="p1", published_at=aware, content_hash="b"),
    ]
    volume, velocity, _ = compute_rss_reach_per_person(articles)["p1"]
    # 2 articles spanning 1h → rate=2/h → 2/5 = 0.4
    assert volume == 2
    assert velocity == pytest.approx(0.4, rel=1e-3)


def test_articles_without_published_at_are_skipped_for_velocity():
    """A NULL published_at rules the article out of velocity calc;
    volume still counts so the article does not vanish from reach."""
    now = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    articles = [
        _gold(person_id="p1", published_at=now, content_hash="a"),
        _gold(person_id="p1", published_at=None, content_hash="b"),
    ]
    volume, velocity, _ = compute_rss_reach_per_person(articles)["p1"]
    assert volume == 2
    # Only one usable timestamp → falls back to the < 2 floor.
    assert velocity == 0.1


def test_engagement_is_always_zero():
    now = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    articles = [
        _gold(
            person_id="p1",
            published_at=now + timedelta(minutes=m),
            content_hash=f"h{m}",
        )
        for m in range(20)
    ]
    _, _, engagement = compute_rss_reach_per_person(articles)["p1"]
    assert engagement == 0
