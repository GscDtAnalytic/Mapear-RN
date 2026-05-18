"""Golden set — 25 exemplos reais extraídos do dataset mapear_consolidated_20260423.

CONTRATO: estes 25 casos são contratuais com o seed RN atual (dbt/seeds/rn_targets.csv +
mapear-domain/src/mapear_domain/seeds/rn/aliases.json). Uma quebra após mudança de seed
pode ser regressão de seed, não de matcher — verificar diff do seed antes de
culpar o RegionMatcher.

Os 7 xfails se dividem em três grupos:

  Grupo A — Gap de seed (C6):
    C6 · Doutor Severiano não está no seed porque não tem conta monitorada.
    Não é cidade de escopo. xfail permanente enquanto não houver decisão de inclusão.
    Ver análise em docs/proposal_deterministic_matcher.md §3.4.

  Grupo B — Limitação do matcher (C8):
    C8 · Underscore em handles (@cmei_apodi) impede word-boundary \\b.
    '_ é \\w' no Python regex — \\bapodi\\b não casa 'cmei_apodi'.
    Workaround futuro: pré-processar handles (remover @prefixo_) antes do matching.
    Ver docs/proposal_deterministic_matcher.md §C8-limitacao.

  Grupo C — Resolução por autor, não por texto (M3, M4, M9, M10):
    Esses posts não contêm o nome do político no texto — só na conta que publicou.
    Serão resolvidos na Fase 2 via region.get_politician_by_handle(platform, handle).
    Ver item 1.3 do Sprint 1 / decisão de escopo em ORCHESTRATION.md.

  Grupo D — Comportamento correto (M8):
    M8 · Fátima Alves é prefeita de Coronel João Pessoa, cidade sem conta monitorada.
    Ela não está em rn_targets.csv; o matcher não deve detectá-la. Esperado.
"""

import time

import pytest
from mapear_domain.region import load_region

from mapear_nlp.matchers.region_matcher import RegionMatcher


@pytest.fixture(scope="module")
def rn_matcher() -> RegionMatcher:
    return RegionMatcher(load_region("rn"))


# ---------------------------------------------------------------------------
# Cidades — C1–C10
# ---------------------------------------------------------------------------


def test_C1_coronel_joao_pessoa(rn_matcher: RegionMatcher):
    """C1: "Escola Municipal Coronel João Pessoa" → cidade detectada."""
    text = "TEJE assinado o convênio para a reforma e ampliação da Escola Municipal Coronel João Pessoa!"
    result = rn_matcher.match(text)
    assert "Coronel João Pessoa" in result.mentioned_cities


def test_C2_pau_dos_ferros(rn_matcher: RegionMatcher):
    """C2: "entreguei mais uma conquista para Pau dos Ferros" → cidade detectada."""
    text = "Meu povo, hoje entreguei mais uma conquista para Pau dos Ferros: a pavimentação da Rua Frei Galvão"
    result = rn_matcher.match(text)
    assert "Pau dos Ferros" in result.mentioned_cities


def test_C3_pau_dos_ferros_premio(rn_matcher: RegionMatcher):
    """C3: menção de Pau dos Ferros em contexto de prêmio."""
    text = "Meu povo, que orgulho de Pau dos Ferros! Fomos reconhecidos com o XIII Prêmio Sebrae Prefeitura Empreendedora"
    result = rn_matcher.match(text)
    assert "Pau dos Ferros" in result.mentioned_cities


def test_C4_goianinha(rn_matcher: RegionMatcher):
    """C4: "programação da Festa de Nossa Senhora da Guia, em Goianinha" → detectada."""
    text = "Participei ontem da primeira noite de shows dentro da programação da Festa de Nossa Senhora da Guia, em Goianinha..."
    result = rn_matcher.match(text)
    assert "Goianinha" in result.mentioned_cities


def test_C5_coronel_joao_pessoa_via_role_pattern(rn_matcher: RegionMatcher):
    """C5: "prefeita Fátima Alves, de Coronel João Pessoa" → cidade via role_pattern."""
    text = "Hoje também estive com Pachica, que representava a prefeita Fátima Alves, de Coronel João Pessoa, para dialogarmos..."
    result = rn_matcher.match(text)
    assert "Coronel João Pessoa" in result.mentioned_cities


@pytest.mark.xfail(
    # Grupo A — gap de seed. Doutor Severiano não tem conta monitorada em rn_targets.csv
    # e não foi incluída em aliases.json. Sem conta monitorada = fora do escopo de
    # cobertura do matcher. Não é bug; é decisão de escopo. Ver §3.4 do proposal doc.
    reason="Doutor Severiano não está no seed; não é cidade monitorada",
    strict=True,
)
def test_C6_doutor_severiano_not_in_seed(rn_matcher: RegionMatcher):
    text = "ao lado da prefeita Maria de Fátima para conversar sobre a Escola Municipal Coronel João Pessoa, em Doutor Severiano!"
    result = rn_matcher.match(text)
    assert "Doutor Severiano" in result.mentioned_cities


def test_C7_apodi_explicit(rn_matcher: RegionMatcher):
    """C7: menção direta de Apodi → detectada (já estava nos aliases)."""
    text = "Participamos hoje de um dia de ações sociais... contribuindo na obra do Senhor... em Apodi..."
    result = rn_matcher.match(text)
    assert "Apodi" in result.mentioned_cities


@pytest.mark.xfail(
    # Grupo B — limitação do matcher. '_ é \w' no Python regex: \bapodi\b exige
    # que o caractere anterior a 'a' seja \W, mas '_' é \w, então a boundary falha.
    # Workaround futuro: pré-processar texto para stripping de handles sociais
    # (ex.: substituir @\w+_(\w+) pela parte após o underscore) antes do matching.
    # Reavaliar após Fase 3 com dados reais. Ver §C8-limitacao no proposal doc.
    reason="C8: @cmei_apodi — underscore em handle impede word-boundary \\b; "
    "limitação conhecida: _ é \\w no regex Python",
    strict=True,
)
def test_C8_apodi_via_handle_underscore(rn_matcher: RegionMatcher):
    """C8: "estivemos na @cmei_apodi" — underscore bloqueia \bapodi\b."""
    text = "estivemos na @cmei_apodi onde abrimos juntos, a sala de leitura..."
    result = rn_matcher.match(text)
    assert "Apodi" in result.mentioned_cities


def test_C9_pau_dos_ferros_historia(rn_matcher: RegionMatcher):
    """C9: referência histórica à vitória em Pau dos Ferros."""
    text = "Quem lembra desta foto? Ela foi registrada em 2020... após a minha primeira vitória como prefeita de Pau dos Ferros"
    result = rn_matcher.match(text)
    assert "Pau dos Ferros" in result.mentioned_cities


def test_C10_pau_dos_ferros_prefeitura(rn_matcher: RegionMatcher):
    """C10: "Prefeitura de Pau dos Ferros" → detectada."""
    text = "Meu povo, ao chegar na sede da Prefeitura de Pau dos Ferros, fui recebida com tanto carinho pelos nossos servidores..."
    result = rn_matcher.match(text)
    assert "Pau dos Ferros" in result.mentioned_cities


# ---------------------------------------------------------------------------
# Prefeitos — M1–M10
# ---------------------------------------------------------------------------


def test_M1_nilda_via_dotted_handle(rn_matcher: RegionMatcher):
    """M1: "@professora.nilda" — ponto como separador permite word-boundary."""
    text = (
        "estive em audiência com a prefeita @professora.nilda e secretário de Turismo"
    )
    result = rn_matcher.match(text)
    assert "Raimunda Nilda" in result.mentioned_mayors


def test_M2_paulinho_standalone(rn_matcher: RegionMatcher):
    """M2: "Paulinho" standalone como apelido → Paulinho Freire."""
    text = "desapropriei 50 hectares de terra para criar o distrito empresarial. Paguei quatro parcelas, e Paulinho concluiu..."
    result = rn_matcher.match(text)
    assert "Paulinho Freire" in result.mentioned_mayors


@pytest.mark.xfail(
    # Grupo C — resolução por autor. O texto não contém "Allyson" — o prefeito só
    # é identificável pelo author_handle da conta que publicou. Será resolvido na
    # Fase 2 via region.get_politician_by_handle(platform, author_handle).
    reason="M3: texto não menciona 'Allyson' — detecção requer conta de origem",
    strict=True,
)
def test_M3_allyson_no_name_in_text(rn_matcher: RegionMatcher):
    """M3: post de Allyson Silva sem menção ao nome no texto."""
    text = "COMPARTILHA PRA TODO MUNDO VER... Uma história de superação... por Mossoró. O povo chegou na prefeitura..."
    result = rn_matcher.match(text)
    assert "Allyson Silva" in result.mentioned_mayors


@pytest.mark.xfail(
    # Grupo C — resolução por autor. Mesmo grupo de M3. Ver comentário em M3.
    reason="M4: texto sem menção nominal da prefeita — só contexto de cargo",
    strict=True,
)
def test_M4_nilda_no_name_in_text(rn_matcher: RegionMatcher):
    """M4: post de Nilda sem menção do nome."""
    text = "Ser prefeita vai muito além de ocupar um cargo. É estar presente, ouvir de perto..."
    result = rn_matcher.match(text)
    assert "Raimunda Nilda" in result.mentioned_mayors


def test_M5_nilda_tiktok(rn_matcher: RegionMatcher):
    """M5: "NILDA PREFEITA 77" → detectada pelo alias standalone."""
    text = "É A MAIOR CARREATA DA HISTÓRIA DE PARNAMIRIM MOSTRANDO QUE O POVO QUER NILDA PREFEITA 77!"
    result = rn_matcher.match(text)
    assert "Raimunda Nilda" in result.mentioned_mayors


def test_M6_nilda_possessiva(rn_matcher: RegionMatcher):
    """M6: "a nossa Nilda" → detectada."""
    text = "é o povo confirmando a mudança que a nossa Nilda vai trazer para Parnamirim"
    result = rn_matcher.match(text)
    assert "Raimunda Nilda" in result.mentioned_mayors


def test_M7_paulinho_tiktok(rn_matcher: RegionMatcher):
    """M7: "virada que Paulinho vai dar" → Paulinho Freire."""
    text = "vamos construir a Natal do futuro! A maior virada que Paulinho vai dar"
    result = rn_matcher.match(text)
    assert "Paulinho Freire" in result.mentioned_mayors


@pytest.mark.xfail(
    # Grupo D — comportamento correto. Fátima Alves é prefeita de Coronel João Pessoa,
    # cidade sem conta monitorada em rn_targets.csv. O matcher não deve e não pode
    # detectá-la. Este xfail documenta o comportamento esperado — não é limitação.
    reason="M8: Fátima Alves não é monitored; alias 'fatima alves' não está no seed",
    strict=True,
)
def test_M8_fatima_alves_coronel(rn_matcher: RegionMatcher):
    """M8: "prefeita Fátima Alves" → não está no seed (cidade sem conta monitorada)."""
    text = "Hoje também estive com Pachica, que representava a prefeita Fátima Alves, de Coronel João Pessoa..."
    result = rn_matcher.match(text)
    assert "Fátima Alves" in result.mentioned_mayors


@pytest.mark.xfail(
    # Grupo C — resolução por autor. Mesmo grupo de M3. Ver comentário em M3.
    reason="M9: texto de Mariann Almeida sem menção nominal — só contexto",
    strict=True,
)
def test_M9_mariann_no_name_in_text(rn_matcher: RegionMatcher):
    """M9: post de Mariann sem menção do nome."""
    text = "Celebrar vitórias e fortalecer parcerias! Nosso encontro reuniu prefeitos, prefeitas e aliados..."
    result = rn_matcher.match(text)
    assert "Mariann Almeida" in result.mentioned_mayors


@pytest.mark.xfail(
    # Grupo C — resolução por autor. Mesmo grupo de M3. Ver comentário em M3.
    reason="M10: post de Emídio sobre quilombola sem menção nominal",
    strict=True,
)
def test_M10_emidio_no_name_in_text(rn_matcher: RegionMatcher):
    """M10: post de Emídio sobre Macaíba sem mencionar 'Emídio'."""
    text = "SABIA QUE A MAIOR COMUNIDADE QUILOMBOLA DO RN FICA EM MACAÍBA?! Olha quanta força... 👏"
    result = rn_matcher.match(text)
    assert "Emídio Júnior" in result.mentioned_mayors


# ---------------------------------------------------------------------------
# Falsos positivos — FP1–FP5
# ---------------------------------------------------------------------------


def test_FP4_fatima_alias_removed(rn_matcher: RegionMatcher):
    """FP4: 'prefeita Fátima Alves' NÃO deve disparar Fátima Bezerra (governadora).
    Alias 'fátima' e 'fatima' foram removidos de governor_aliases.
    """
    text = "Hoje estive com a prefeita Fátima Alves, de Coronel João Pessoa"
    result = rn_matcher.match(text)
    assert "Fátima Bezerra" not in result.mentioned_governors


def test_FP5_paulo_freire_alias_removed(rn_matcher: RegionMatcher):
    """FP5: texto sobre educação com 'Paulo Freire' NÃO deve disparar Paulinho Freire.
    Alias 'paulo freire' foi removido de mayor_aliases.
    """
    text = "A pedagogia de Paulo Freire transformou a educação brasileira nas décadas de 60 e 70."
    result = rn_matcher.match(text)
    assert "Paulinho Freire" not in result.mentioned_mayors


def test_allyson_bezerra_detected_as_candidate(rn_matcher: RegionMatcher):
    """'Allyson Bezerra' é detectado como candidato a governador (não mais alias direto de prefeito).
    Nota: 'allyson' standalone ainda dispara Allyson Silva via alias — aceito per proposta §4.4.
    O teste relevante é que 'allyson bezerra' como alias DIRETO de Allyson Silva foi removido.
    """
    text = "O deputado Allyson Bezerra quer disputar o governo do RN"
    result = rn_matcher.match(text)
    assert "Allyson Bezerra" in result.mentioned_candidates


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


_SHORT_POST = (
    "Paulinho entregou mais obras em Natal hoje, disse o governador em Mossoró."
)
_LONG_ARTICLE = (
    """
O prefeito de Natal, Paulinho Freire, anunciou nesta terça-feira um pacote de
investimentos para a capital potiguar. A governadora Fátima Bezerra participou
do anúncio ao lado do vice-governador Walter Alves. O secretário de obras
confirmou que os trabalhos começam em Mossoró na próxima semana, com extensão
prevista para Parnamirim, São Gonçalo do Amarante e Macaíba. O senador Rogério
Marinho criticou o anúncio, enquanto o candidato Cadu Xavier aproveitou para
fazer campanha em Caicó e Açu. A prefeita de Parnamirim, Raimunda Nilda, disse
que aguarda os recursos. Em Ceará-Mirim, Antonio Henrique Câmara Bezerra pediu
prioridade para a BR-406. O prefeito de Caicó, Judas Tadeu, também se
manifestou. Em Pau dos Ferros, Mariann Almeida comemorou o anúncio. O prefeito
Emídio de Macaíba e a prefeita de Extremoz, Jussara Sales, também participaram
da reunião. Em Currais Novos, Lucas Galvão da Cruz recebeu a equipe do governo.
"""
    * 3
)  # ~1800 chars


def test_performance_short_post_under_5ms(rn_matcher: RegionMatcher):
    """Post curto (~70 chars) deve completar em <5ms."""
    start = time.perf_counter()
    for _ in range(100):
        rn_matcher.match(_SHORT_POST)
    elapsed_ms = (time.perf_counter() - start) / 100 * 1000
    assert elapsed_ms < 5.0, f"match() levou {elapsed_ms:.2f}ms (limite: 5ms)"


def test_performance_long_article_under_20ms(rn_matcher: RegionMatcher):
    """Artigo longo (~1800 chars) deve completar em <20ms."""
    start = time.perf_counter()
    for _ in range(50):
        rn_matcher.match(_LONG_ARTICLE)
    elapsed_ms = (time.perf_counter() - start) / 50 * 1000
    assert elapsed_ms < 20.0, f"match() levou {elapsed_ms:.2f}ms (limite: 20ms)"
