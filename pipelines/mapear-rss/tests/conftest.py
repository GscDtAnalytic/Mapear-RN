"""Shared test fixtures."""

from collections.abc import Generator

import pytest

from mapear_domain.region import load_region
from mapear_domain.rn_entities import set_region


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force local environment for all tests."""
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("LOG_FORMAT", "text")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    # NERExtractor / SentimentAnalyzer load_region(settings.mapear_region)
    # inside __init__, so the autouse `_inject_test_region` fixture below
    # (which only swaps the module-level region) is not enough — anything
    # that instantiates a region-aware class needs MAPEAR_REGION=test too.
    monkeypatch.setenv("MAPEAR_REGION", "test")


@pytest.fixture(autouse=True)
def _inject_test_region() -> Generator[None, None, None]:
    """Inject the synthetic test Region for all tests.

    Prevents tests from loading the real RN production seed (dbt/seeds/)
    and makes test results independent of production data changes.
    Also clears rn_filter's keyword cache so it rebuilds from the test Region.
    """
    from mapear_rss.discovery import rn_filter

    set_region(load_region("test"))
    rn_filter._keyword_index.cache_clear()
    yield
    set_region(None)
    rn_filter._keyword_index.cache_clear()


@pytest.fixture
def sample_article_data() -> dict:
    """Return a minimal valid article dict for testing."""
    return {
        "url": "https://example.com/noticia-teste",
        "source_feed": "test_feed",
        "title": "Prefeito de Testópolis anuncia nova obra",
        "content": "O prefeito João Teste anunciou investimentos em infraestrutura.",
        "author": "Repórter Teste",
        "content_hash": "abc123def456",
    }
