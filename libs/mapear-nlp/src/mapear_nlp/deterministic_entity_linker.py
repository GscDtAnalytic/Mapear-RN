"""Linkagem determinística de entidades políticas do RN.

Complementa o NER com correspondência insensível a acentos, baseada em
dicionário YAML canônico. Detecta cidades, prefeitos e governadores mesmo
quando o texto usa variantes sem acento ou apelidos.

Funcionalidades:
- Matching por word boundary em texto normalizado (sem acento, lowercase)
- Inferência de cidade a partir de prefeito reconhecido
- Padrão contextual "prefeit[oa] NOME, de CIDADE" → liga pessoa e cidade
- Trace de resolução opcional (debug=True) explicando cada link
"""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from loguru import logger
from mapear_domain.region import Region


def _dict_candidates(filename: str) -> list[Path]:
    """Candidate paths for a dictionary file, repo-layout-aware."""
    return [
        Path(f"data/dictionaries/{filename}"),
        Path(f"../data/dictionaries/{filename}"),
        Path(__file__).parents[3] / "data" / "dictionaries" / filename,
    ]


_DICT_CACHE: "dict | None" = None


def _find_dict_path(filename: str = "rn_entities.yml") -> Path | None:
    for p in _dict_candidates(filename):
        if p.exists():
            return p
    return None


def normalize_for_match(text: str) -> str:
    """Lowercase, strip diacritics — prepara texto para matching sem acento.

    Exemplos:
        "Mossoró"     → "mossoro"
        "Fátima"      → "fatima"
        "NATAL"       → "natal"
        "João Câmara" → "joao camara"
    """
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _build_alternation(aliases: "dict[str, str]") -> "re.Pattern[str] | None":
    """Compila regex de alternação a partir de um mapa alias→canônico.

    Aliases mais longos ficam primeiro para evitar match parcial guloso.
    Aplicado sobre texto já normalizado (sem acento, lowercase).
    """
    if not aliases:
        return None
    escaped = sorted(
        (re.escape(a) for a in aliases),
        key=len,
        reverse=True,
    )
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b")


# Padrão contextual: "prefeita|prefeito NOME, de|em CIDADE"
# Aplicado em texto normalizado (sem acento, lowercase).
# Requer vírgula entre NOME e preposição para evitar ambiguidade com
# nomes compostos que contêm "de" (ex: "José de Figueiredo").
_ROLE_CITY_PATTERN = re.compile(
    r"\bprefeit[oa]\s+"
    r"(\S+(?:\s+\S+){0,4}?)"  # NOME: 1–5 tokens, lazy
    r"\s*,\s*"  # vírgula obrigatória como separador
    r"(?:de|em)\s+"  # preposição de localização
    r"(\S+(?:\s+\S+){0,5})",  # CIDADE: até 6 tokens
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
class LinkResult:
    """Resultado da linkagem determinística para um texto."""

    mentioned_cities: list[str] = field(default_factory=list)
    mentioned_mayors: list[str] = field(default_factory=list)
    mentioned_governors: list[str] = field(default_factory=list)
    resolution_trace: "list[ResolutionTrace] | None" = None


class DeterministicEntityLinker:
    """Linker determinístico insensível a acentos para entidades políticas do RN.

    Uso básico:
        linker = DeterministicEntityLinker()
        result = linker.link("prefeita Fátima Alves, de Coronel João Pessoa")
        # result.mentioned_cities → ["Coronel João Pessoa"]
        # result.mentioned_mayors → ["Fátima Alves"]

    Com trace de resolução (debug):
        result = linker.link(text, debug=True)
        for t in result.resolution_trace:
            print(t.field, t.canonical, t.match_type)
    """

    def __init__(
        self,
        dict_path: "Path | None" = None,
        region: "Region | None" = None,
    ) -> None:
        # If region is given but dict_path is not, derive the canonical
        # `{region.id}_entities.yml` filename. Explicit dict_path always wins.
        if dict_path is None and region is not None:
            dict_path = _find_dict_path(f"{region.id}_entities.yml")
        self._dict_path = dict_path
        self._region_id = region.id if region is not None else None
        self._loaded = False
        # alias normalizado → canônico
        self._city_aliases: dict[str, str] = {}
        self._mayor_aliases: dict[str, str] = {}
        self._governor_aliases: dict[str, str] = {}
        # prefeito canônico → cidade canônica
        self._mayor_to_city: dict[str, str] = {}
        # person_id → cidade canônica
        self._person_id_to_city: dict[str, str] = {}
        # padrões compilados
        self._city_pattern: re.Pattern[str] | None = None
        self._mayor_pattern: re.Pattern[str] | None = None
        self._governor_pattern: re.Pattern[str] | None = None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        # Fallback chain: explicit dict_path → region-derived → rn_entities.yml.
        path = self._dict_path
        if path is None:
            fallback_name = (
                f"{self._region_id}_entities.yml"
                if self._region_id
                else "rn_entities.yml"
            )
            path = _find_dict_path(fallback_name)
        if path is None or not path.exists():
            logger.warning(
                "Dictionary not found for region={region} — "
                "DeterministicEntityLinker will return empty results. "
                "Verify data/dictionaries/.",
                region=self._region_id or "rn",
            )
            self._loaded = True
            return

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        for city_entry in data.get("cities", []):
            canonical_city: str = city_entry["canonical"]
            # Adiciona o próprio canônico como alias
            self._city_aliases[normalize_for_match(canonical_city)] = canonical_city
            for alias in city_entry.get("aliases", []):
                self._city_aliases[normalize_for_match(alias)] = canonical_city

            mayor_entry = city_entry.get("mayor") or {}
            canonical_mayor: str | None = mayor_entry.get("canonical")
            if canonical_mayor:
                self._mayor_to_city[canonical_mayor] = canonical_city
                norm_mayor = normalize_for_match(canonical_mayor)
                self._mayor_aliases[norm_mayor] = canonical_mayor
                for alias in mayor_entry.get("aliases", []):
                    self._mayor_aliases[normalize_for_match(alias)] = canonical_mayor
                person_id: str | None = mayor_entry.get("person_id")
                if person_id:
                    self._person_id_to_city[person_id] = canonical_city

        for gov_entry in data.get("governors", []):
            canonical_gov: str = gov_entry["canonical"]
            self._governor_aliases[normalize_for_match(canonical_gov)] = canonical_gov
            for alias in gov_entry.get("aliases", []):
                self._governor_aliases[normalize_for_match(alias)] = canonical_gov

        self._city_pattern = _build_alternation(self._city_aliases)
        self._mayor_pattern = _build_alternation(self._mayor_aliases)
        self._governor_pattern = _build_alternation(self._governor_aliases)

        self._loaded = True
        logger.debug(
            "DeterministicEntityLinker carregado: {nc} alias cidades, "
            "{nm} alias prefeitos, {ng} alias governadores",
            nc=len(self._city_aliases),
            nm=len(self._mayor_aliases),
            ng=len(self._governor_aliases),
        )

    # ------------------------------------------------------------------
    # Funções de extração pública
    # ------------------------------------------------------------------

    def extract_mentioned_cities(self, text: str) -> list[str]:
        """Extrai cidades mencionadas no texto (insensível a acento)."""
        return self.link(text).mentioned_cities

    def extract_mentioned_mayors(self, text: str) -> list[str]:
        """Extrai prefeitos mencionados no texto (insensível a acento)."""
        return self.link(text).mentioned_mayors

    def extract_mentioned_governors(self, text: str) -> list[str]:
        """Extrai governadores/políticos mencionados (insensível a acento)."""
        return self.link(text).mentioned_governors

    def get_city_for_person_id(self, person_id: str) -> str | None:
        """Retorna a cidade canônica associada a um person_id de prefeito."""
        self._ensure_loaded()
        return self._person_id_to_city.get(person_id)

    # ------------------------------------------------------------------
    # Método principal
    # ------------------------------------------------------------------

    def link(self, text: str, debug: bool = False) -> LinkResult:
        """Executa linkagem determinística em um texto.

        Etapas:
        1. Matching direto de aliases de cidades no texto normalizado.
        2. Matching direto de aliases de prefeitos + inferência de cidade.
        3. Matching direto de aliases de governadores/políticos.
        4. Padrão contextual "prefeit[oa] NOME, de CIDADE".

        Args:
            text:  Texto de entrada (post, artigo, etc.).
            debug: Se True, popula `resolution_trace` no resultado.

        Returns:
            LinkResult com listas deduplicadas de entidades canônicas.
        """
        self._ensure_loaded()

        norm_text = normalize_for_match(text)
        trace: list[ResolutionTrace] | None = [] if debug else None

        # canonical → alias que gerou o match (primeiro encontrado)
        cities_found: dict[str, str] = {}
        mayors_found: dict[str, str] = {}
        governors_found: dict[str, str] = {}

        # --- 1. Cidades ---
        if self._city_pattern:
            for m in self._city_pattern.finditer(norm_text):
                alias = m.group(0)
                canonical = self._city_aliases.get(alias, "")
                if canonical and canonical not in cities_found:
                    cities_found[canonical] = alias
                    if debug and trace is not None:
                        ctx_start = max(0, m.start() - 20)
                        trace.append(
                            ResolutionTrace(
                                field="city",
                                canonical=canonical,
                                matched_alias=alias,
                                match_type="direct",
                                context=norm_text[ctx_start : m.end() + 20],
                            )
                        )

        # --- 2. Prefeitos + inferência de cidade ---
        if self._mayor_pattern:
            for m in self._mayor_pattern.finditer(norm_text):
                alias = m.group(0)
                canonical = self._mayor_aliases.get(alias, "")
                if canonical and canonical not in mayors_found:
                    mayors_found[canonical] = alias
                    if debug and trace is not None:
                        ctx_start = max(0, m.start() - 20)
                        trace.append(
                            ResolutionTrace(
                                field="mayor",
                                canonical=canonical,
                                matched_alias=alias,
                                match_type="direct",
                                context=norm_text[ctx_start : m.end() + 20],
                            )
                        )
                    # Cidade inferida a partir do prefeito
                    inferred_city = self._mayor_to_city.get(canonical, "")
                    if inferred_city and inferred_city not in cities_found:
                        cities_found[inferred_city] = (
                            f"<inferred_from_mayor:{canonical}>"
                        )
                        if debug and trace is not None:
                            trace.append(
                                ResolutionTrace(
                                    field="city",
                                    canonical=inferred_city,
                                    matched_alias=canonical,
                                    match_type="inferred",
                                )
                            )

        # --- 3. Governadores / políticos ---
        if self._governor_pattern:
            for m in self._governor_pattern.finditer(norm_text):
                alias = m.group(0)
                canonical = self._governor_aliases.get(alias, "")
                if canonical and canonical not in governors_found:
                    governors_found[canonical] = alias
                    if debug and trace is not None:
                        ctx_start = max(0, m.start() - 20)
                        trace.append(
                            ResolutionTrace(
                                field="governor",
                                canonical=canonical,
                                matched_alias=alias,
                                match_type="direct",
                                context=norm_text[ctx_start : m.end() + 20],
                            )
                        )

        # --- 4. Padrão contextual "prefeit[oa] NOME, de CIDADE" ---
        for m in _ROLE_CITY_PATTERN.finditer(norm_text):
            person_frag = m.group(1).strip().rstrip(",")
            city_frag = m.group(2).strip()

            # Verifica se o fragmento de cidade contém alias conhecido
            linked_city: str | None = None
            if self._city_pattern:
                cm = self._city_pattern.search(city_frag)
                if cm:
                    linked_city = self._city_aliases.get(cm.group(0))

            if linked_city is None:
                continue  # cidade não reconhecida — ignora o match

            if linked_city not in cities_found:
                cities_found[linked_city] = city_frag
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

            # Liga a pessoa como prefeito: verifica alias primeiro, senão usa
            # capitalização do fragmento como canônico de fallback.
            canonical_mayor = self._mayor_aliases.get(person_frag)
            if canonical_mayor is None:
                canonical_mayor = " ".join(w.capitalize() for w in person_frag.split())
            if canonical_mayor not in mayors_found:
                mayors_found[canonical_mayor] = person_frag
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

        return LinkResult(
            mentioned_cities=list(cities_found.keys()),
            mentioned_mayors=list(mayors_found.keys()),
            mentioned_governors=list(governors_found.keys()),
            resolution_trace=trace,
        )


def merge_with_ner(
    ner_result: dict,
    link_result: LinkResult,
) -> dict:
    """Une resultado do linker ao dict do NERExtractor.extract_from_text().

    Preserva todas as entidades do NER e acrescenta as novas do linker
    sem duplicatas (comparação case-insensitive).
    """

    def _union(existing: list[str], new: list[str]) -> list[str]:
        seen = {v.lower() for v in existing}
        merged = list(existing)
        for item in new:
            if item.lower() not in seen:
                seen.add(item.lower())
                merged.append(item)
        return merged

    return {
        **ner_result,
        "mentioned_cities": _union(
            ner_result.get("mentioned_cities", []),
            link_result.mentioned_cities,
        ),
        "mentioned_mayors": _union(
            ner_result.get("mentioned_mayors", []),
            link_result.mentioned_mayors,
        ),
        "mentioned_governors": _union(
            ner_result.get("mentioned_governors", []),
            link_result.mentioned_governors,
        ),
    }
