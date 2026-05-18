"""Testes unitários do RegionMatcher — Region sintética, sem I/O de arquivo."""

import pytest
from mapear_domain.region import Politician, Region

from mapear_nlp.matchers.region_matcher import (
    RegionMatcher,
    normalize_for_match,
)

# ---------------------------------------------------------------------------
# Fixture: Region sintética com dados controlados
# ---------------------------------------------------------------------------


@pytest.fixture()
def region() -> Region:
    return Region(
        id="synthetic",
        cities_mayors_rows=[
            {
                "city": "Testópolis",
                "state": "TX",
                "mayor": "Prefeito Silva",
                "party": "PT",
                "monitored": "true",
            },
            {
                "city": "Vilafake",
                "state": "TX",
                "mayor": "Prefeita Souza",
                "party": "MDB",
                "monitored": "true",
            },
            {
                "city": "Açu",
                "state": "TX",
                "mayor": "Dr. Lula",
                "party": "Republicanos",
                "monitored": "true",
            },
        ],
        governor_rows=[
            {
                "name": "Governador Teste",
                "role": "governor",
                "state": "TX",
                "party": "PT",
                "term_start": "2023",
                "term_end": "2026",
            }
        ],
        city_aliases={
            "testopolis": "Testópolis",
            "acu": "Açu",
            "assu": "Açu",
        },
        mayor_aliases={
            "silva": "Prefeito Silva",
            "dr. lula": "Dr. Lula",
            "dr lula": "Dr. Lula",
        },
        governor_aliases={
            "governador teste": "Governador Teste",
            "gov teste": "Governador Teste",
        },
        politician_aliases={
            "senador x": "Senador X",
        },
        politicians=[
            Politician(
                person_id="gov_teste",
                name="Governador Teste",
                role="governor",
                aliases=["Gov. Teste"],
                handles={},
                is_incumbent=True,
            ),
            Politician(
                person_id="cand_bob",
                name="Bob Candidato",
                role="governor_candidate",
                aliases=["Bobinho"],
                handles={},
            ),
            Politician(
                person_id="senator_x",
                name="Senador X",
                role="senator",
                aliases=["Sen. X"],
                handles={},
            ),
        ],
    )


@pytest.fixture()
def matcher(region: Region) -> RegionMatcher:
    return RegionMatcher(region)


# ---------------------------------------------------------------------------
# normalize_for_match
# ---------------------------------------------------------------------------


def test_normalize_strips_diacritics():
    assert normalize_for_match("Mossoró") == "mossoro"
    assert normalize_for_match("Fátima") == "fatima"
    assert normalize_for_match("João Câmara") == "joao camara"


def test_normalize_lowercases():
    assert normalize_for_match("NATAL") == "natal"
    assert normalize_for_match("Testópolis") == "testopolis"


# ---------------------------------------------------------------------------
# Construtor — fail loud
# ---------------------------------------------------------------------------


def test_init_fails_on_empty_region():
    empty = Region(id="empty")
    with pytest.raises(ValueError, match="zero cidades e zero aliases"):
        RegionMatcher(empty)


def test_init_succeeds_with_city_aliases_only():
    r = Region(id="minimal", city_aliases={"natal": "Natal"})
    m = RegionMatcher(r)  # não deve levantar
    assert m is not None


# ---------------------------------------------------------------------------
# Matching de cidades
# ---------------------------------------------------------------------------


def test_match_city_canonical_name(matcher: RegionMatcher):
    result = matcher.match("Obras em Testópolis hoje")
    assert "Testópolis" in result.mentioned_cities


def test_match_city_by_alias(matcher: RegionMatcher):
    result = matcher.match("Visita a testopolis foi produtiva")
    assert "Testópolis" in result.mentioned_cities


def test_match_city_accent_insensitive(matcher: RegionMatcher):
    result = matcher.match("obras em Acu concluídas")
    assert "Açu" in result.mentioned_cities


def test_match_city_alias_assu(matcher: RegionMatcher):
    result = matcher.match("a cidade de Assu recebeu")
    assert "Açu" in result.mentioned_cities


def test_match_city_case_insensitive(matcher: RegionMatcher):
    result = matcher.match("TESTÓPOLIS registra crescimento")
    assert "Testópolis" in result.mentioned_cities


def test_match_city_word_boundary_no_false_positive(matcher: RegionMatcher):
    result = matcher.match("testopolisense não é Testópolis")
    # "testopolisense" não deve gerar match extra — "Testópolis" do final sim
    assert result.mentioned_cities == ["Testópolis"]


def test_match_empty_text_returns_empty(matcher: RegionMatcher):
    result = matcher.match("")
    assert result.mentioned_cities == []
    assert result.mentioned_mayors == []
    assert result.mentioned_governors == []
    assert result.mentioned_candidates == []
    assert result.mentioned_politicians == []
    assert result.mentioned_parties == []


# ---------------------------------------------------------------------------
# Matching de prefeitos + inferência de cidade
# ---------------------------------------------------------------------------


def test_match_mayor_canonical(matcher: RegionMatcher):
    result = matcher.match("Prefeito Silva inaugurou o hospital")
    assert "Prefeito Silva" in result.mentioned_mayors


def test_match_mayor_by_alias(matcher: RegionMatcher):
    result = matcher.match("silva assinou o convênio")
    assert "Prefeito Silva" in result.mentioned_mayors


def test_match_mayor_infers_city(matcher: RegionMatcher):
    result = matcher.match("silva anunciou investimentos")
    assert "Prefeito Silva" in result.mentioned_mayors
    assert "Testópolis" in result.mentioned_cities


def test_match_mayor_dr_lula_alias(matcher: RegionMatcher):
    result = matcher.match("Dr. Lula entregou pavimentação")
    assert "Dr. Lula" in result.mentioned_mayors
    assert "Açu" in result.mentioned_cities


def test_match_mayor_dr_lula_no_dot(matcher: RegionMatcher):
    result = matcher.match("dr lula falou sobre obras em Açu")
    assert "Dr. Lula" in result.mentioned_mayors


# ---------------------------------------------------------------------------
# Matching de governadores
# ---------------------------------------------------------------------------


def test_match_governor_canonical(matcher: RegionMatcher):
    result = matcher.match("Governador Teste discursou ontem")
    assert "Governador Teste" in result.mentioned_governors


def test_match_governor_by_alias(matcher: RegionMatcher):
    result = matcher.match("gov teste anunciou investimentos")
    assert "Governador Teste" in result.mentioned_governors


def test_match_governor_not_in_mayors(matcher: RegionMatcher):
    result = matcher.match("gov teste falou ao povo")
    assert "Governador Teste" not in result.mentioned_mayors


# ---------------------------------------------------------------------------
# Matching de candidatos
# ---------------------------------------------------------------------------


def test_match_candidate_by_name(matcher: RegionMatcher):
    result = matcher.match("Bob Candidato lidera pesquisas")
    assert "Bob Candidato" in result.mentioned_candidates


def test_match_candidate_by_alias(matcher: RegionMatcher):
    result = matcher.match("Bobinho vence debate")
    assert "Bob Candidato" in result.mentioned_candidates


def test_candidate_not_in_governors(matcher: RegionMatcher):
    result = matcher.match("Bobinho quer governar")
    assert "Bob Candidato" not in result.mentioned_governors


# ---------------------------------------------------------------------------
# Matching de políticos (senadores, etc.)
# ---------------------------------------------------------------------------


def test_match_politician_by_alias(matcher: RegionMatcher):
    result = matcher.match("senador x discursa no plenário")
    assert "Senador X" in result.mentioned_politicians


def test_match_politician_by_canonical(matcher: RegionMatcher):
    result = matcher.match("Senador X votou contra")
    assert "Senador X" in result.mentioned_politicians


# ---------------------------------------------------------------------------
# Matching de partidos
# ---------------------------------------------------------------------------


def test_match_party_exact(matcher: RegionMatcher):
    result = matcher.match("venceu o PT nas eleições")
    assert "PT" in result.mentioned_parties


def test_match_party_mdb(matcher: RegionMatcher):
    result = matcher.match("candidato do MDB lidera")
    assert "MDB" in result.mentioned_parties


# ---------------------------------------------------------------------------
# Padrão contextual "prefeit[oa] NOME, de CIDADE"
# ---------------------------------------------------------------------------


def test_role_city_pattern_links_city(matcher: RegionMatcher):
    result = matcher.match("prefeita Souza, de Vilafake, entregou obras")
    assert "Vilafake" in result.mentioned_cities


def test_role_city_pattern_links_mayor(matcher: RegionMatcher):
    result = matcher.match("prefeita Souza, de Vilafake, entregou obras")
    assert "Prefeita Souza" in result.mentioned_mayors


def test_role_city_pattern_prefeito_variant(matcher: RegionMatcher):
    result = matcher.match("prefeito Silva, em Testópolis, discursou")
    assert "Testópolis" in result.mentioned_cities


# ---------------------------------------------------------------------------
# Debug trace
# ---------------------------------------------------------------------------


def test_debug_trace_populated(matcher: RegionMatcher):
    result = matcher.match("silva falou em Testópolis", debug=True)
    assert result.resolution_trace is not None
    fields = {t.field for t in result.resolution_trace}
    assert "mayor" in fields
    assert "city" in fields


def test_debug_trace_none_when_not_requested(matcher: RegionMatcher):
    result = matcher.match("silva falou")
    assert result.resolution_trace is None


def test_debug_trace_has_inferred_city(matcher: RegionMatcher):
    result = matcher.match("silva anunciou", debug=True)
    inferred = [t for t in result.resolution_trace if t.match_type == "inferred"]  # type: ignore[union-attr]
    assert len(inferred) == 1
    assert inferred[0].canonical == "Testópolis"


# ---------------------------------------------------------------------------
# Deduplicação
# ---------------------------------------------------------------------------


def test_city_deduplicated_across_aliases(matcher: RegionMatcher):
    result = matcher.match("Testópolis e testopolis crescem")
    assert result.mentioned_cities.count("Testópolis") == 1


def test_mayor_deduplicated(matcher: RegionMatcher):
    result = matcher.match("Prefeito Silva e silva são a mesma pessoa")
    assert result.mentioned_mayors.count("Prefeito Silva") == 1
