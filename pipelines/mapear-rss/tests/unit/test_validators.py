"""Tests for Pydantic validation schemas."""

import pytest
from pydantic import ValidationError

from mapear_domain.models.base import DiscoveredURL, FeedSource, RawArticle


class TestFeedSource:
    def test_valid_feed(self) -> None:
        feed = FeedSource(
            name="Tribuna do Norte",
            url="https://tribunadonorte.com.br/feed/",
            category="rn_local",
            priority=8,
            is_rn_focused=True,
        )
        assert feed.name == "Tribuna do Norte"
        assert feed.is_rn_focused is True

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValidationError):
            FeedSource(name="bad", url="not-a-url")

    def test_priority_bounds(self) -> None:
        with pytest.raises(ValidationError):
            FeedSource(name="x", url="https://example.com", priority=11)


class TestRawArticle:
    def test_valid_article(self, sample_article_data: dict) -> None:
        article = RawArticle(**sample_article_data)
        assert article.content_hash == "abc123def456"
        assert article.schema_version == 1

    def test_missing_required_field(self, sample_article_data: dict) -> None:
        del sample_article_data["content_hash"]
        with pytest.raises(ValidationError):
            RawArticle(**sample_article_data)


class TestDiscoveredURL:
    def test_auto_timestamp(self) -> None:
        url = DiscoveredURL(
            url="https://example.com/noticia",
            source_feed="test",
        )
        assert url.discovered_at is not None
