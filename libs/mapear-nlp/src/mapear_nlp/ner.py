"""Named Entity Recognition (NER) extractor with RN entity prioritization.

Uses spaCy (local mode) or Google Cloud Natural Language API (api mode)
for general NER and overlays the RN entity dictionary to identify
mentions of monitored cities, mayors, and parties.
Produces SilverArticle objects with entity annotations and
the is_rn_relevant flag.
"""

import re

from loguru import logger
from mapear_domain.models.base import RawArticle, SilverArticle
from mapear_domain.region import Region, load_region
from mapear_infra.config import EnrichmentMode, get_settings

from mapear_nlp.ner_postprocessing import is_noise_person_entity
from mapear_nlp.transformation.cleaner import clean_text

# Noise patterns for entity filtering — removes URLs, handles, hashtags,
# social media links, and other non-informative entities from YouTube
# descriptions and RSS boilerplate.
_NOISE_PATTERNS = [
    re.compile(r"^https?://", re.IGNORECASE),
    re.compile(r"^@"),
    re.compile(r"^#"),
    re.compile(r"^http", re.IGNORECASE),
    re.compile(
        r"facebook|instagram|tiktok|twitter|whatsapp|kwai|youtube|panflix",
        re.IGNORECASE,
    ),
    re.compile(
        r"Thumb:|Cupom|CUPOM|app$|canal$|site$|forma$|objetivo$",
        re.IGNORECASE,
    ),
]

# Entity labels that carry no analytical value and should be dropped.
# OTHER: catch-all label do GCP NL v2.
# NUMBER/DATE/PHONE_NUMBER/PRICE/ADDRESS: dados estruturados, não entidades
# úteis para o domínio sociopolítico.
# EVENT/WORK_OF_ART/CONSUMER_GOOD: ruído nos vídeos do YouTube (títulos de
# álbuns, nomes de programas) que polui a contagem de entidades sem agregar
# sinal sobre cidades, pessoas ou organizações monitoradas.
_SKIP_LABELS = {
    "OTHER",
    "NUMBER",
    "DATE",
    "PHONE_NUMBER",
    "PRICE",
    "ADDRESS",
    "EVENT",
    "WORK_OF_ART",
    "CONSUMER_GOOD",
}

# Generic role descriptors that map to the current incumbent governor's
# canonical name. Region-agnostic regexes (no entity name embedded) so
# the table can be shared across regions; the canonical names below
# come from each region's incumbent governor seed.
#
# TECH-DEBT (Stage 2A): role labels stay Portuguese-only. Cross-region
# expansion may want a `Region.coref_role_patterns` field — left as a
# follow-up; not in 2A's scope.
_GOVERNOR_ROLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:a\s+|o\s+)?governador(?:a)?\b", re.IGNORECASE),
    re.compile(r"\bgoverno\s+do\s+(?:estado|rn|estado\s+do\s+\w+)\b", re.IGNORECASE),
]


def _build_coref_patterns(region: Region) -> list[tuple[re.Pattern[str], str]]:
    """Build co-reference resolution patterns for a region.

    Maps role descriptions like "a governadora" → incumbent governor,
    "o prefeito de Mossoró" → mayor of Mossoró, etc. Re-evaluated per
    NERExtractor instance so a different `MAPEAR_REGION` swaps the
    canonical names without code changes.
    """
    patterns: list[tuple[re.Pattern[str], str]] = []

    # Governor co-references — map every governor-role descriptor to
    # the current incumbent canonical name(s).
    incumbents = sorted(region.get_incumbent_governor_names())
    if incumbents:
        incumbent_name = incumbents[0]  # one canonical per region by design
        for role_pat in _GOVERNOR_ROLE_PATTERNS:
            patterns.append((role_pat, incumbent_name))

    # Mayor co-references from seed data.
    for row in region.load_seed_data():
        city = row.get("city", "")
        mayor = row.get("mayor", "")
        if not city or not mayor or "sub judice" in mayor.lower():
            continue
        # "o prefeito de Natal" → "Paulinho Freire"
        pattern = re.compile(
            rf"\b(?:o\s+)?prefeit[oa]\s+de\s+{re.escape(city)}\b", re.IGNORECASE
        )
        patterns.append((pattern, mayor))

    return patterns


def _resolve_coreferences(
    text: str, coref_patterns: list[tuple[re.Pattern[str], str]]
) -> list[str]:
    """Find person mentions via co-reference patterns in text.

    Returns list of canonical person names found via role descriptions.
    """
    found: list[str] = []
    for pattern, person in coref_patterns:
        if pattern.search(text) and person not in found:
            found.append(person)
    return found


def _build_mentioned_persons(
    text: str,
    entities: list[dict[str, str]],
    mentioned_mayors: list[str],
    mentioned_governors: list[str],
    region: Region,
    coref_patterns: list[tuple[re.Pattern[str], str]],
) -> list[str]:
    """Build a deduplicated list of all mentioned persons.

    Combines: known mayors + governors + co-reference resolution +
    PERSON entities from spaCy/GCP that passed noise filtering.
    """
    persons: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        canonical = region.normalize_entity(name)
        key = canonical.lower()
        if key not in seen:
            seen.add(key)
            persons.append(canonical)

    # Known RN persons from dictionary matching
    for name in mentioned_mayors:
        _add(name)
    for name in mentioned_governors:
        _add(name)

    # Co-reference resolution: "a governadora" → incumbent governor name
    for name in _resolve_coreferences(text, coref_patterns):
        _add(name)

    # PERSON entities from NER that aren't noise
    for ent in entities:
        if ent.get("label") in ("PER", "PERSON"):
            _add(ent["text"])

    return persons


def _build_word_pattern(words: set[str]) -> re.Pattern[str] | None:
    """Build a single compiled regex that matches any word with boundaries."""
    if not words:
        return None
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(rf"\b(?:{'|'.join(escaped)})\b", re.IGNORECASE)


def _filter_entities(
    entities: list[dict[str, str]],
    _stats: list[int] | None = None,
) -> list[dict[str, str]]:
    """Remove noisy entities: URLs, handles, hashtags, generic roles, OTHER label.

    Applied after spaCy/GCP extraction before entities reach Silver/Gold layers.

    Args:
        entities: Raw entity list from spaCy or GCP.
        _stats:   Optional two-element list ``[person_noise, total_person]``
                  that is mutated in-place for batch metric aggregation.
                  Callers that don't need metrics can omit this argument.
    """
    filtered = []
    person_noise = 0
    total_person = 0

    for ent in entities:
        text = ent.get("text", "").strip()
        label = ent.get("label", "")

        if not text or len(text) < 2:
            continue

        if label in _SKIP_LABELS:
            continue

        if any(pat.search(text) for pat in _NOISE_PATTERNS):
            continue

        is_person = label in ("PER", "PERSON")
        if is_person:
            total_person += 1
            if is_noise_person_entity(text, label):
                person_noise += 1
                continue

        filtered.append(ent)

    if _stats is not None:
        _stats[0] += person_noise
        _stats[1] += total_person

    return filtered


class NERExtractor:
    """Extracts named entities and flags region-relevant articles.

    Region DI (Stage 2A): ``region`` is the source of truth for every
    entity-name lookup. When omitted, falls back to
    ``load_region(settings.mapear_region)`` for backward compatibility
    with code that has not been ported yet. New call sites should pass
    ``region`` explicitly so tests and multi-region pipelines stay
    isolated from the process-wide settings.
    """

    def __init__(self, region: Region | None = None) -> None:
        settings = get_settings()
        self._nlp = None
        self._gcp_client = None
        self._mode = settings.enrichment_mode
        self._skip = settings.enrichment_mode == EnrichmentMode.SKIP

        if region is None:
            region = load_region(settings.mapear_region)
        self._region = region

        self.rn_cities = region.get_city_names()
        self.rn_mayors = region.get_mayor_names()
        # Wide set: incumbent + ex-governors + declared candidates +
        # outras figuras políticas no plano federal. Usado para
        # popular ``mentioned_governors`` (sinal fraco).
        self.rn_governors = (
            region.get_governor_names() | region.get_governor_candidate_names()
        )
        # Narrow set: apenas o(a) governador(a) em exercício. Sinal forte
        # de relevância — citação isolada de candidato/senador não
        # basta para flagear o artigo, mas o titular sim.
        self.rn_governors_incumbent = region.get_incumbent_governor_names()
        self.rn_parties = region.get_party_names()

        self._city_pattern = _build_word_pattern(self.rn_cities)
        self._mayor_pattern = _build_word_pattern(self.rn_mayors)
        self._governor_pattern = _build_word_pattern(self.rn_governors)
        self._incumbent_governor_pattern = _build_word_pattern(
            self.rn_governors_incumbent
        )
        self._party_pattern = _build_word_pattern(self.rn_parties)
        self._coref_patterns = _build_coref_patterns(region)

    @property
    def nlp(self):  # noqa: ANN201
        """Lazy-load spaCy model."""
        if self._nlp is None and not self._skip and self._mode == EnrichmentMode.LOCAL:
            try:
                import spacy

                model_name = get_settings().spacy_model
                logger.info("Loading spaCy model: {model}", model=model_name)
                self._nlp = spacy.load(model_name)
            except ImportError:
                logger.error(
                    "spaCy not installed — generic NER will be empty. "
                    "Install with: pip install spacy"
                )
                self._skip = True
            except OSError:
                model_name = get_settings().spacy_model
                logger.error(
                    "spaCy model '{model}' not found — generic NER will be empty. "
                    "Install with: python -m spacy download {model}",
                    model=model_name,
                )
                self._skip = True
        return self._nlp

    @property
    def gcp_client(self):  # noqa: ANN201
        """Lazy-load Google Cloud Natural Language client."""
        if self._gcp_client is None and self._mode == EnrichmentMode.API:
            from google.cloud import language_v2

            self._gcp_client = language_v2.LanguageServiceClient()
            logger.info("Initialized GCP Natural Language API client for NER")
        return self._gcp_client

    def _find_matches(
        self,
        pattern: re.Pattern[str] | None,
        text: str,
        entities: set[str],
        normalize: bool = True,
    ) -> list[str]:
        """Find all entity matches in text using pre-compiled pattern."""
        if pattern is None:
            return []
        found = set(pattern.findall(text))
        found_lower = {f.lower() for f in found}
        results = []
        for entity in entities:
            if entity.lower() in found_lower:
                results.append(
                    self._region.normalize_entity(entity) if normalize else entity
                )
        return results

    def _extract_entities_gcp(self, text: str) -> list[dict[str, str]]:
        """Extract entities using Google Cloud Natural Language API."""
        from google.cloud import language_v2

        try:
            document = language_v2.Document(
                content=text[:5000],
                type_=language_v2.Document.Type.PLAIN_TEXT,
                language_code="pt",
            )
            response = self.gcp_client.analyze_entities(request={"document": document})
            return [
                {"text": entity.name, "label": entity.type_.name}
                for entity in response.entities
            ]
        except Exception as e:
            logger.warning("GCP NER failed: {error}", error=str(e))
            return []

    def extract(
        self,
        article: RawArticle,
        _stats: list[int] | None = None,
    ) -> SilverArticle:
        """Run NER on a raw article and produce a SilverArticle.

        Args:
            article: Raw article to process.
            _stats:  Optional two-element list ``[person_noise, total_person]``
                     forwarded to ``_filter_entities`` for batch metric tracking.
        """
        content_clean = clean_text(article.content)

        entities: list[dict[str, str]] = []
        if self._mode == EnrichmentMode.API:
            entities = self._extract_entities_gcp(content_clean)
        elif self.nlp is not None:
            doc = self.nlp(content_clean)
            entities = [{"text": ent.text, "label": ent.label_} for ent in doc.ents]

        entities = _filter_entities(entities, _stats=_stats)

        full_text = f"{article.title} {content_clean}"

        mentioned_cities = self._find_matches(
            self._city_pattern, full_text, self.rn_cities
        )
        mentioned_mayors = self._find_matches(
            self._mayor_pattern, full_text, self.rn_mayors
        )
        mentioned_governors = self._find_matches(
            self._governor_pattern, full_text, self.rn_governors
        )
        mentioned_parties = self._find_matches(
            self._party_pattern, full_text, self.rn_parties, normalize=False
        )

        incumbent_match = bool(
            self._incumbent_governor_pattern
            and self._incumbent_governor_pattern.search(full_text)
        )
        is_rn_relevant = bool(mentioned_cities or mentioned_mayors or incumbent_match)

        # Build mentioned_persons: union of mayors + governors + co-references
        mentioned_persons = _build_mentioned_persons(
            full_text,
            entities,
            mentioned_mayors,
            mentioned_governors,
            self._region,
            self._coref_patterns,
        )

        if not entities and self._mode != EnrichmentMode.SKIP:
            logger.warning(
                "NER produced no generic entities for: {title}",
                title=article.title[:80],
            )

        return SilverArticle(
            url=article.url,
            source_feed=article.source_feed,
            title=article.title,
            content_clean=content_clean,
            author=article.author,
            published_at=article.published_at,
            extracted_at=article.extracted_at,
            content_hash=article.content_hash,
            entities=entities,
            mentioned_cities=mentioned_cities,
            mentioned_mayors=mentioned_mayors,
            mentioned_governors=mentioned_governors,
            mentioned_parties=mentioned_parties,
            mentioned_persons=mentioned_persons,
            is_rn_relevant=is_rn_relevant,
            source_type=getattr(article, "source_type", "rss"),
        )

    def extract_from_text(self, text: str) -> dict:
        """Run NER on raw text without requiring a RawArticle.

        Designed for consumers that build their own text from heterogeneous
        sources (e.g. YouTube: title + description + transcript).

        Returns a dict with entity/person fields plus two quality metrics:
        - ``ner_noise_filtered_count``: PERSON entities removed as generic noise.
        - ``entities_person_removed_as_noise_pct``: percentage of PERSON entities
          that were noise (0.0–100.0).
        """
        entities: list[dict[str, str]] = []
        if self._mode == EnrichmentMode.API:
            entities = self._extract_entities_gcp(text)
        elif self.nlp is not None:
            doc = self.nlp(text)
            entities = [{"text": ent.text, "label": ent.label_} for ent in doc.ents]

        noise_stats: list[int] = [0, 0]  # [person_noise, total_person_before]
        entities = _filter_entities(entities, _stats=noise_stats)
        noise_count, total_person = noise_stats
        noise_pct = noise_count / total_person * 100 if total_person else 0.0

        mentioned_cities = self._find_matches(self._city_pattern, text, self.rn_cities)
        mentioned_mayors = self._find_matches(self._mayor_pattern, text, self.rn_mayors)
        mentioned_governors = self._find_matches(
            self._governor_pattern, text, self.rn_governors
        )
        mentioned_parties = self._find_matches(
            self._party_pattern, text, self.rn_parties, normalize=False
        )

        incumbent_match = bool(
            self._incumbent_governor_pattern
            and self._incumbent_governor_pattern.search(text)
        )
        is_rn_relevant = bool(mentioned_cities or mentioned_mayors or incumbent_match)

        mentioned_persons = _build_mentioned_persons(
            text,
            entities,
            mentioned_mayors,
            mentioned_governors,
            self._region,
            self._coref_patterns,
        )

        if not entities and self._mode != EnrichmentMode.SKIP:
            logger.debug(
                "NER produced no generic entities for text ({len} chars)",
                len=len(text),
            )

        return {
            "entities": entities,
            "mentioned_cities": mentioned_cities,
            "mentioned_mayors": mentioned_mayors,
            "mentioned_governors": mentioned_governors,
            "mentioned_parties": mentioned_parties,
            "mentioned_persons": mentioned_persons,
            "is_rn_relevant": is_rn_relevant,
            "ner_noise_filtered_count": noise_count,
            "entities_person_removed_as_noise_pct": round(noise_pct, 2),
        }

    def extract_batch(
        self,
        articles: list[RawArticle],
        rn_feed_urls: set[str] | None = None,
    ) -> list[SilverArticle]:
        """Process a batch of raw articles through NER.

        Args:
            articles: Raw articles to process.
            rn_feed_urls: Optional set of feed URLs that are RN-focused.
                Used for the summary log breakdown (RN feeds vs national).
        """
        from collections import Counter

        # investigar acoplamento de mapear-infra.metrics com prometheus_client
        from mapear_infra.metrics import (
            ner_content_relevant,
            ner_duration,
            track_duration,
        )

        results = []
        rn_count = 0
        # Batch noise stats: [person_noise_count, total_person_before]
        noise_stats: list[int] = [0, 0]

        with track_duration(ner_duration):
            for article in articles:
                silver = self.extract(article, _stats=noise_stats)
                results.append(silver)
                if silver.is_rn_relevant:
                    rn_count += 1
                    ner_content_relevant.inc()

                logger.info(
                    "NER article: {title} | feed={feed} | "
                    "cities={cities} mayors={mayors} governors={governors} | "
                    "relevant={relevant}",
                    title=article.title[:80],
                    feed=article.source_feed,
                    cities=silver.mentioned_cities,
                    mayors=silver.mentioned_mayors,
                    governors=silver.mentioned_governors,
                    relevant=silver.is_rn_relevant,
                )

        # Quality metrics
        ner_noise_filtered_count = noise_stats[0]
        total_person_before = noise_stats[1]
        entities_person_removed_as_noise_pct = (
            ner_noise_filtered_count / total_person_before * 100
            if total_person_before
            else 0.0
        )
        logger.info(
            "NER noise quality: {noise} PERSON entities removed as noise "
            "({pct:.1f}% of {total} PERSON entities seen)",
            noise=ner_noise_filtered_count,
            pct=entities_person_removed_as_noise_pct,
            total=total_person_before,
        )

        # Detailed summary with breakdown by feed type
        all_cities: Counter[str] = Counter()
        for s in results:
            for city in s.mentioned_cities:
                all_cities[city] += 1
        top_entities = all_cities.most_common(10)

        if rn_feed_urls is not None:
            rn_feed_articles = [s for s in results if s.source_feed in rn_feed_urls]
            nat_feed_articles = [
                s for s in results if s.source_feed not in rn_feed_urls
            ]
            rn_from_rn = sum(1 for s in rn_feed_articles if s.is_rn_relevant)
            rn_from_nat = sum(1 for s in nat_feed_articles if s.is_rn_relevant)
            logger.info(
                "NER relevance: {rn_from_rn}/{rn_total} from RN feeds, "
                "{rn_from_nat}/{nat_total} from national feeds. "
                "Top geo entities: {entities}",
                rn_from_rn=rn_from_rn,
                rn_total=len(rn_feed_articles),
                rn_from_nat=rn_from_nat,
                nat_total=len(nat_feed_articles),
                entities=top_entities,
            )
        else:
            logger.info(
                "NER batch: {total} articles, {rn} RN-relevant. "
                "Top geo entities: {entities}",
                total=len(results),
                rn=rn_count,
                entities=top_entities,
            )

        return results
