"""Tests for text cleaning."""

from mapear_nlp.transformation.cleaner import clean_text


class TestCleanText:
    def test_removes_boilerplate(self) -> None:
        text = (
            "O prefeito anunciou investimentos em saúde.\n"
            "Leia também: Governo federal libera verba.\n"
            "Mais detalhes na próxima semana."
        )
        result = clean_text(text)
        assert "Leia também" not in result
        assert "prefeito anunciou" in result

    def test_removes_social_media_boilerplate(self) -> None:
        text = (
            "Texto principal da notícia sobre educação no estado.\n"
            "Siga o portal no Instagram @portal"
        )
        result = clean_text(text)
        assert "Instagram" not in result

    def test_normalizes_whitespace(self) -> None:
        text = "Texto   com    muitos   espaços"
        result = clean_text(text)
        assert "   " not in result

    def test_fixes_encoding(self) -> None:
        text = "Texto\u00a0com\u200bnon-breaking\u00a0spaces"
        result = clean_text(text)
        assert "\u00a0" not in result
        assert "\u200b" not in result

    def test_preserves_paragraphs(self) -> None:
        text = (
            "Primeiro parágrafo com conteúdo suficiente para teste.\n\n"
            "Segundo parágrafo com conteúdo suficiente para teste."
        )
        result = clean_text(text)
        assert "\n\n" in result
