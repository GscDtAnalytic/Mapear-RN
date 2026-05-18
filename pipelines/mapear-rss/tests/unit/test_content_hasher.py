"""Tests for content hashing and normalization."""

from mapear_rss.extraction.content_hasher import hash_content, normalize_text


class TestNormalizeText:
    def test_removes_accents(self) -> None:
        assert "natal" in normalize_text("Natal")

    def test_removes_punctuation(self) -> None:
        result = normalize_text("Hello, world!")
        assert "," not in result
        assert "!" not in result

    def test_collapses_whitespace(self) -> None:
        result = normalize_text("foo   bar\t\tbaz")
        assert result == "foo bar baz"

    def test_lowercases(self) -> None:
        result = normalize_text("PREFEITO DE NATAL")
        assert result == "prefeito de natal"


class TestHashContent:
    def test_same_content_same_hash(self) -> None:
        h1 = hash_content("Title", "Content body here")
        h2 = hash_content("Title", "Content body here")
        assert h1 == h2

    def test_different_content_different_hash(self) -> None:
        h1 = hash_content("Title A", "Content A")
        h2 = hash_content("Title B", "Content B")
        assert h1 != h2

    def test_formatting_differences_same_hash(self) -> None:
        h1 = hash_content("Prefeito anuncia obra", "Texto do artigo completo.")
        h2 = hash_content("Prefeito  anuncia  obra", "Texto do artigo completo.")
        assert h1 == h2

    def test_returns_hex_string(self) -> None:
        result = hash_content("t", "c")
        assert len(result) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in result)
