"""Tests for article deduplication."""

import pytest

from mapear_domain.models.base import RawArticle
from mapear_rss.transformation.deduplicator import Deduplicator


@pytest.fixture
def make_article():
    """Factory for creating test RawArticle objects."""

    def _make(url: str = "https://example.com/1", content_hash: str = "hash1"):
        return RawArticle(
            url=url,
            source_feed="test",
            title="Test Article",
            content="Test content for the article body.",
            content_hash=content_hash,
        )

    return _make


class TestDeduplicator:
    def test_no_duplicates(self, make_article) -> None:
        articles = [
            make_article(url="https://example.com/1", content_hash="aaa"),
            make_article(url="https://example.com/2", content_hash="bbb"),
        ]
        dedup = Deduplicator()
        result = dedup.deduplicate(articles)
        assert len(result) == 2

    def test_removes_intra_batch_duplicates(self, make_article) -> None:
        articles = [
            make_article(url="https://example.com/1", content_hash="same"),
            make_article(url="https://example.com/2", content_hash="same"),
        ]
        dedup = Deduplicator()
        result = dedup.deduplicate(articles)
        assert len(result) == 1

    def test_removes_known_hash_duplicates(self, make_article) -> None:
        known = {"existing_hash"}
        articles = [
            make_article(content_hash="existing_hash"),
            make_article(url="https://example.com/2", content_hash="new_hash"),
        ]
        dedup = Deduplicator(known_hashes=known)
        result = dedup.deduplicate(articles)
        assert len(result) == 1
        assert result[0].content_hash == "new_hash"

    def test_updates_known_hashes(self, make_article) -> None:
        dedup = Deduplicator()
        articles = [make_article(content_hash="abc")]
        dedup.deduplicate(articles)
        assert "abc" in dedup.known_hashes

    def test_empty_list(self) -> None:
        dedup = Deduplicator()
        result = dedup.deduplicate([])
        assert result == []
