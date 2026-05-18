"""Matcher determinístico para entidades políticas de uma Region.

Substitui o DeterministicEntityLinker, que falhava silenciosamente em produção
por não encontrar o YAML de dicionário em runtime. O RegionMatcher recebe uma
Region já carregada — zero I/O em runtime após a inicialização.

Fail loud: se a Region não tiver cidades nem aliases, levanta ValueError no
construtor. Não existe "rodar sem dados" como modo legítimo de operação.

Uso:
    from mapear_domain.region import load_region
    from mapear_nlp.matchers import RegionMatcher

    matcher = RegionMatcher(load_region("rn"))
    result = matcher.match("Paulinho entregou obras em Natal hoje")
    # result.mentioned_mayors  → ["Paulinho Freire"]
    # result.mentioned_cities  → ["Natal"]
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from loguru import logger
from mapear_domain.region import Region


def normalize_for_match(text: str) -> str:
    """Lowercase + strip diacritics para matching sem acento.

    Exemplos:
        "Mossoró"     → "mossoro"
        "NATAL"       → "natal"
        "João Câmara" → "joao camara"
    """
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _build_alternation(aliases: dict[str, str]) -> re.Pattern[str] | None:
    """Compila regex de alternação alias→canonical.

    Aliases mais longos ficam primeiro (greedy seguro). Aplicado em texto
    já normalizado (normalize_for_match).
    """
    if not aliases:
        return None
    escaped = sorted((re.escape(a) for a in aliases), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b")


# Limitação conhecida — underscore em handles sociais (@conta_cidade):
# Python trata '_' como \w, então \bapodi\b não casa em '@cmei_apodi' porque
# o '_' antes de 'apodi' não é uma word boundary. Workaround futuro: strip de
# handles antes do matching (ex.: re.sub(r'@\w+', '', text)). Ver teste C8 em
# test_region_matcher_golden.py e §C8-limitacao em
# docs/proposal_deterministic_matcher.md.

# "prefeit[oa] NOME, de|em CIDADE" — padrão contextual para ligação pessoa+cidade.
# Requer vírgula entre NOME e preposição para evitar nomes compostos com "de"
# (ex.: "José de Figueiredo").
# Cobertura documentada (ver §role-pattern no proposal doc):
#   - "prefeit[oa] NOME, de CIDADE" → cobre
#   - "prefeit[oa] NOME, em CIDADE" → cobre
#   - Casos de ordem invertida ("prefeita de X, NOME") e nome antes do papel
#     ("NOME, prefeita de X") geralmente funcionam via matching direto (alias),
#     não por este padrão. O padrão complementa; não é o único caminho.
_ROLE_CITY_PATTERN = re.compile(
    r"\bprefeit[oa]\s+"
    r"(\S+(?:\s+\S+){0,4}?)"
    r"\s*,\s*"
    r"(?:de|em)\s+"
    r"(\S+(?:\s+\S+){0,5})",
)


@dataclass
class ResolutionTrace:
    """Detalhe de uma entidade linkada — expõe o porquê para debug."""

    field: str
    canonical: str
    matched_alias: str
    match_type: str  # "direct" | "inferred" | "role_pattern"
    context: str = ""


@dataclass
class MatchResult:
    """Resultado do matching determinístico para um texto."""

    mentioned_cities: list[str] = field(default_factory=list)
    mentioned_mayors: list[str] = field(default_factory=list)
    mentioned_governors: list[str] = field(default_factory=list)
    mentioned_candidates: list[str] = field(default_factory=list)
    mentioned_politicians: list[str] = field(default_factory=list)
    mentioned_parties: list[str] = field(default_factory=list)
    resolution_trace: list[ResolutionTrace] | None = None


class RegionMatcher:
    """Matcher determinístico insensível a acentos para entidades de uma Region.

    Construído a partir de uma Region já carregada. Stateless após inicialização —
    pode ser compartilhado entre threads.

    Fail loud no construtor: se a Region tiver zero cidades e zero aliases,
    levanta ValueError imediatamente. Não existe caso legítimo de operar sem dados.
    """

    def __init__(self, region: Region) -> None:
        if not region.cities_mayors_rows and not region.city_aliases:
            raise ValueError(
                f"Region '{region.id}' tem zero cidades e zero aliases — "
                "RegionMatcher não pode operar. Verifique que cities_mayors.csv "
                "e aliases.json foram carregados corretamente via load_region()."
            )

        # -- city aliases: chaves do JSON + nomes canônicos do CSV --
        city_aliases: dict[str, str] = {}
        for key, canonical in region.city_aliases.items():
            city_aliases[normalize_for_match(key)] = canonical
        for name in region.get_city_names():
            city_aliases[normalize_for_match(name)] = name

        # -- mayor aliases: chaves do JSON + nomes canônicos do CSV --
        mayor_aliases: dict[str, str] = {}
        for key, canonical in region.mayor_aliases.items():
            mayor_aliases[normalize_for_match(key)] = canonical
        for name in region.get_mayor_names():
            mayor_aliases[normalize_for_match(name)] = name

        # -- mayor → city: inferência a partir de prefeito reconhecido --
        mayor_to_city: dict[str, str] = {}
        for row in region.cities_mayors_rows:
            if row.get("mayor") and row.get("city"):
                mayor_to_city[row["mayor"]] = row["city"]

        # -- governadores vs candidatos: separação por papel no politicians --
        candidate_names = {
            p.name for p in region.politicians if p.role == "governor_candidate"
        }

        governor_aliases: dict[str, str] = {}
        candidate_aliases: dict[str, str] = {}

        for key, canonical in region.governor_aliases.items():
            nkey = normalize_for_match(key)
            if canonical in candidate_names:
                candidate_aliases[nkey] = canonical
            else:
                governor_aliases[nkey] = canonical

        # Adiciona nomes canônicos dos governadores incumbentes
        for name in region.get_incumbent_governor_names():
            governor_aliases[normalize_for_match(name)] = name

        # Candidatos: nome + aliases do targets.csv
        for pol in region.politicians:
            if pol.role == "governor_candidate":
                candidate_aliases[normalize_for_match(pol.name)] = pol.name
                for alias in pol.aliases:
                    if alias:
                        candidate_aliases[normalize_for_match(alias)] = pol.name

        # -- politician aliases: senadores, deputados, vice-governadores --
        politician_aliases: dict[str, str] = {}
        for key, canonical in region.politician_aliases.items():
            politician_aliases[normalize_for_match(key)] = canonical
        for pol in region.politicians:
            if pol.role in ("senator", "deputy_federal", "vice_governor"):
                politician_aliases[normalize_for_match(pol.name)] = pol.name
                for alias in pol.aliases:
                    if alias:
                        politician_aliases[normalize_for_match(alias)] = pol.name

        # -- party aliases --
        party_aliases: dict[str, str] = {}
        for party in region.get_party_names():
            if party:
                party_aliases[normalize_for_match(party)] = party

        self._city_aliases = city_aliases
        self._mayor_aliases = mayor_aliases
        self._mayor_to_city = mayor_to_city
        self._governor_aliases = governor_aliases
        self._candidate_aliases = candidate_aliases
        self._politician_aliases = politician_aliases
        self._party_aliases = party_aliases

        self._city_pattern = _build_alternation(city_aliases)
        self._mayor_pattern = _build_alternation(mayor_aliases)
        self._governor_pattern = _build_alternation(governor_aliases)
        self._candidate_pattern = _build_alternation(candidate_aliases)
        self._politician_pattern = _build_alternation(politician_aliases)
        self._party_pattern = _build_alternation(party_aliases)

        logger.debug(
            "RegionMatcher({id}) pronto: {nc} cidades, {nm} prefeitos, "
            "{ng} governadores, {nca} candidatos, {np} políticos, {npa} partidos",
            id=region.id,
            nc=len(city_aliases),
            nm=len(mayor_aliases),
            ng=len(governor_aliases),
            nca=len(candidate_aliases),
            np=len(politician_aliases),
            npa=len(party_aliases),
        )

    # ------------------------------------------------------------------

    def match(self, text: str, debug: bool = False) -> MatchResult:
        """Executa matching determinístico em um texto.

        Etapas:
        1. Cidades — matching direto.
        2. Prefeitos — matching direto + inferência de cidade.
        3. Governadores — matching direto.
        4. Candidatos a governador — matching direto.
        5. Políticos (senadores, deputados, etc.) — matching direto.
        6. Partidos — matching direto.
        7. Padrão contextual "prefeit[oa] NOME, de CIDADE".

        Args:
            text:  Texto de entrada (post, artigo, etc.).
            debug: Se True, popula ``resolution_trace`` no resultado.

        Returns:
            MatchResult com listas deduplicadas de formas canônicas.
        """
        norm = normalize_for_match(text)
        trace: list[ResolutionTrace] | None = [] if debug else None

        cities: dict[str, str] = {}
        mayors: dict[str, str] = {}
        governors: dict[str, str] = {}
        candidates: dict[str, str] = {}
        politicians: dict[str, str] = {}
        parties: dict[str, str] = {}

        # 1. Cidades
        if self._city_pattern:
            for m in self._city_pattern.finditer(norm):
                alias = m.group(0)
                canonical = self._city_aliases.get(alias, "")
                if canonical and canonical not in cities:
                    cities[canonical] = alias
                    if debug and trace is not None:
                        trace.append(
                            ResolutionTrace(
                                field="city",
                                canonical=canonical,
                                matched_alias=alias,
                                match_type="direct",
                                context=norm[max(0, m.start() - 20) : m.end() + 20],
                            )
                        )

        # 2. Prefeitos + inferência de cidade
        if self._mayor_pattern:
            for m in self._mayor_pattern.finditer(norm):
                alias = m.group(0)
                canonical = self._mayor_aliases.get(alias, "")
                if canonical and canonical not in mayors:
                    mayors[canonical] = alias
                    if debug and trace is not None:
                        trace.append(
                            ResolutionTrace(
                                field="mayor",
                                canonical=canonical,
                                matched_alias=alias,
                                match_type="direct",
                                context=norm[max(0, m.start() - 20) : m.end() + 20],
                            )
                        )
                    inferred = self._mayor_to_city.get(canonical, "")
                    if inferred and inferred not in cities:
                        cities[inferred] = f"<inferred_from_mayor:{canonical}>"
                        if debug and trace is not None:
                            trace.append(
                                ResolutionTrace(
                                    field="city",
                                    canonical=inferred,
                                    matched_alias=canonical,
                                    match_type="inferred",
                                )
                            )

        # 3. Governadores
        if self._governor_pattern:
            for m in self._governor_pattern.finditer(norm):
                alias = m.group(0)
                canonical = self._governor_aliases.get(alias, "")
                if canonical and canonical not in governors:
                    governors[canonical] = alias
                    if debug and trace is not None:
                        trace.append(
                            ResolutionTrace(
                                field="governor",
                                canonical=canonical,
                                matched_alias=alias,
                                match_type="direct",
                                context=norm[max(0, m.start() - 20) : m.end() + 20],
                            )
                        )

        # 4. Candidatos
        if self._candidate_pattern:
            for m in self._candidate_pattern.finditer(norm):
                alias = m.group(0)
                canonical = self._candidate_aliases.get(alias, "")
                if canonical and canonical not in candidates:
                    candidates[canonical] = alias
                    if debug and trace is not None:
                        trace.append(
                            ResolutionTrace(
                                field="candidate",
                                canonical=canonical,
                                matched_alias=alias,
                                match_type="direct",
                                context=norm[max(0, m.start() - 20) : m.end() + 20],
                            )
                        )

        # 5. Políticos (senadores, deputados, etc.)
        if self._politician_pattern:
            for m in self._politician_pattern.finditer(norm):
                alias = m.group(0)
                canonical = self._politician_aliases.get(alias, "")
                if canonical and canonical not in politicians:
                    politicians[canonical] = alias
                    if debug and trace is not None:
                        trace.append(
                            ResolutionTrace(
                                field="politician",
                                canonical=canonical,
                                matched_alias=alias,
                                match_type="direct",
                                context=norm[max(0, m.start() - 20) : m.end() + 20],
                            )
                        )

        # 6. Partidos
        if self._party_pattern:
            for m in self._party_pattern.finditer(norm):
                alias = m.group(0)
                canonical = self._party_aliases.get(alias, "")
                if canonical and canonical not in parties:
                    parties[canonical] = alias
                    if debug and trace is not None:
                        trace.append(
                            ResolutionTrace(
                                field="party",
                                canonical=canonical,
                                matched_alias=alias,
                                match_type="direct",
                                context=norm[max(0, m.start() - 20) : m.end() + 20],
                            )
                        )

        # 7. Padrão contextual "prefeit[oa] NOME, de CIDADE"
        for m in _ROLE_CITY_PATTERN.finditer(norm):
            city_frag = m.group(2).strip()
            linked_city: str | None = None
            if self._city_pattern:
                cm = self._city_pattern.search(city_frag)
                if cm:
                    linked_city = self._city_aliases.get(cm.group(0))
            if linked_city is None:
                continue

            if linked_city not in cities:
                cities[linked_city] = city_frag
                if debug and trace is not None:
                    trace.append(
                        ResolutionTrace(
                            field="city",
                            canonical=linked_city,
                            matched_alias=city_frag,
                            match_type="role_pattern",
                            context=m.group(0),
                        )
                    )

            person_frag = m.group(1).strip().rstrip(",")
            canonical_mayor = self._mayor_aliases.get(person_frag)
            if canonical_mayor is None:
                canonical_mayor = " ".join(w.capitalize() for w in person_frag.split())
            if canonical_mayor not in mayors:
                mayors[canonical_mayor] = person_frag
                if debug and trace is not None:
                    trace.append(
                        ResolutionTrace(
                            field="mayor",
                            canonical=canonical_mayor,
                            matched_alias=person_frag,
                            match_type="role_pattern",
                            context=m.group(0),
                        )
                    )

        return MatchResult(
            mentioned_cities=list(cities.keys()),
            mentioned_mayors=list(mayors.keys()),
            mentioned_governors=list(governors.keys()),
            mentioned_candidates=list(candidates.keys()),
            mentioned_politicians=list(politicians.keys()),
            mentioned_parties=list(parties.keys()),
            resolution_trace=trace,
        )
