"""Tests for URL canonicalization."""

import pytest

from mapear_storage.transformation.url_canonicalizer import canonicalize_url


@pytest.mark.parametrize(
    "url,expected",
    [
        # UTM stripping
        (
            "https://tribunadonorte.com.br/artigo?id=123&utm_source=twitter&utm_medium=social",
            "https://tribunadonorte.com.br/artigo?id=123",
        ),
        # fbclid stripping
        (
            "https://agorarn.com.br/news/politica?fbclid=abc123",
            "https://agorarn.com.br/news/politica",
        ),
        # gclid stripping
        (
            "https://example.com/page?gclid=xyz&id=1",
            "https://example.com/page?id=1",
        ),
        # All UTM params removed, clean query params retained
        (
            "https://g1.globo.com/rn/artigo?utm_source=g1&id=99&utm_campaign=home",
            "https://g1.globo.com/rn/artigo?id=99",
        ),
        # Trailing slash on non-root path removed
        (
            "https://tribunadonorte.com.br/artigo/",
            "https://tribunadonorte.com.br/artigo",
        ),
        # Root slash preserved
        (
            "https://tribunadonorte.com.br/",
            "https://tribunadonorte.com.br/",
        ),
        # Non-root with no trailing slash — unchanged
        (
            "https://tribunadonorte.com.br/artigo",
            "https://tribunadonorte.com.br/artigo",
        ),
        # Default HTTPS port 443 stripped
        (
            "https://example.com:443/artigo",
            "https://example.com/artigo",
        ),
        # Default HTTP port 80 stripped
        (
            "http://example.com:80/artigo",
            "http://example.com/artigo",
        ),
        # Non-default port preserved
        (
            "https://example.com:8080/artigo",
            "https://example.com:8080/artigo",
        ),
        # No params — normalize only
        (
            "https://agorarn.com.br/politica/artigo-teste",
            "https://agorarn.com.br/politica/artigo-teste",
        ),
        # Multiple tracking params, all removed, no residual query
        (
            "https://saibamais.jor.br/nota?utm_source=whatsapp&utm_medium=share",
            "https://saibamais.jor.br/nota",
        ),
        # Query params sorted for determinism
        (
            "https://example.com/p?z=last&a=first",
            "https://example.com/p?a=first&z=last",
        ),
        # Portuguese tracking params stripped
        (
            "https://example.com/p?id=1&origem=fb&canal=social",
            "https://example.com/p?id=1",
        ),
    ],
)
def test_canonicalize_url(url: str, expected: str) -> None:
    assert canonicalize_url(url) == expected


def test_same_url_with_different_tracking_same_canonical() -> None:
    base = "https://tribunadonorte.com.br/artigo?id=42"
    with_utm = base + "&utm_source=twitter&utm_medium=social"
    with_fbclid = base + "&fbclid=ABCDEFG"
    assert canonicalize_url(base) == canonicalize_url(with_utm)
    assert canonicalize_url(base) == canonicalize_url(with_fbclid)


def test_different_articles_different_canonical() -> None:
    a = "https://tribunadonorte.com.br/artigo/1?id=1"
    b = "https://tribunadonorte.com.br/artigo/2?id=2"
    assert canonicalize_url(a) != canonicalize_url(b)


def test_canonicalize_url_idempotent() -> None:
    url = "https://agorarn.com.br/politica/nota?id=10"
    assert canonicalize_url(canonicalize_url(url)) == canonicalize_url(url)


def test_canonicalize_url_invalid_input_no_raise() -> None:
    result = canonicalize_url("not-a-url")
    assert isinstance(result, str)
