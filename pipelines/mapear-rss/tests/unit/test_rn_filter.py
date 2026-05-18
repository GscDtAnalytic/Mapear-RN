"""Tests for the discovery-layer keyword filter (BL-08).

Uses the synthetic test Region (seeds/test/) — no dependency on real RN seeds.
The _inject_test_region fixture in conftest.py handles setup/teardown.
"""

import pytest

from mapear_rss.discovery import rn_filter


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    rn_filter._keyword_index.cache_clear()


class TestRNFilter:
    def test_matches_city_name(self) -> None:
        assert rn_filter.matches("Prefeitura de Testópolis anuncia obra", None)

    def test_matches_city_accent_insensitive(self) -> None:
        assert rn_filter.matches("Vereador de Vilafake visita obras", None)
        assert rn_filter.matches("vereador de VILAFAKE", None)

    def test_matches_mayor_name(self) -> None:
        assert rn_filter.matches("João Teste assina decreto", None)

    def test_matches_governor_name(self) -> None:
        assert rn_filter.matches("Governador Teste visita Cidadezinha", None)

    def test_matches_multiword_entity(self) -> None:
        # Multi-word entity matched via substring — behavioral test for BL-08
        assert rn_filter.matches("Pedro Simulado inaugura escola", None)

    def test_matches_rn_sigla_uppercase(self) -> None:
        # _RN_SIGLA is hardcoded in the filter regardless of which region is active
        assert rn_filter.matches("Eleições no RN terão debate", None)
        assert rn_filter.matches("PL-RN lança candidato", None)

    def test_ignores_rn_inside_lowercase_word(self) -> None:
        assert not rn_filter.matches("Governo moderno publica edital", None)
        assert not rn_filter.matches("caderno de esportes", None)

    def test_ignores_unrelated_news(self) -> None:
        assert not rn_filter.matches(
            "São Paulo registra aumento de casos", "Dados da prefeitura paulistana"
        )

    def test_matches_on_description_when_title_empty(self) -> None:
        assert rn_filter.matches(
            "Política regional", "Governador Teste fala sobre orçamento"
        )

    def test_returns_false_for_all_none(self) -> None:
        assert not rn_filter.matches(None, None)

    def test_returns_false_for_all_empty(self) -> None:
        assert not rn_filter.matches("", "", None)

    def test_case_insensitive_city_match(self) -> None:
        assert rn_filter.matches("TESTOPOLIS recebe investimento", None)
        assert rn_filter.matches("testopolis recebe investimento", None)
