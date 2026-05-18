"""NER post-processing: noise filtering for PERSON entities.

Filters generic nouns and political role titles falsely tagged as PERSON
by spaCy/GCP, using an external YAML stoplist with accent-insensitive matching.

The key design guarantee: multi-word proper names (e.g. "Fátima Bezerra")
are always preserved because they cannot match any single-word stoplist entry.
"""

import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

_STOPLIST_PATH = Path(__file__).parent / "ner_stoplist.yaml"
_STOPLIST_CACHE: set[str] | None = None

# Populated lazily on first call to _get_stoplist().
# Exposed for inspection/testing; do not mutate directly.
POLITICAL_STOPLIST_PTBR: set[str] = set()


def normalize_token(text: str) -> str:
    """Lowercase, strip diacritics (NFD decomposition), trim whitespace.

    Examples:
        "Governadora" → "governadora"
        "Mãe"        → "mae"
        "POVO"       → "povo"
        "secretária" → "secretaria"
    """
    text = text.lower().strip()
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _get_stoplist(path: Path | None = None) -> set[str]:
    """Return the normalized stoplist, loading from YAML on first call.

    Args:
        path: Override path for testing with a custom YAML file.
    """
    global _STOPLIST_CACHE, POLITICAL_STOPLIST_PTBR
    if _STOPLIST_CACHE is not None and path is None:
        return _STOPLIST_CACHE

    stoplist_path = path or _STOPLIST_PATH
    with open(stoplist_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    terms: set[str] = set()
    for category_terms in data.values():
        if isinstance(category_terms, list):
            for term in category_terms:
                terms.add(normalize_token(str(term)))

    if path is None:
        _STOPLIST_CACHE = terms
        # Mutate in-place so that `from ... import POLITICAL_STOPLIST_PTBR`
        # references remain valid after lazy load.
        POLITICAL_STOPLIST_PTBR.clear()
        POLITICAL_STOPLIST_PTBR.update(terms)
    return terms


def reset_stoplist_cache() -> None:
    """Evict the cached stoplist (use in tests or after config changes)."""
    global _STOPLIST_CACHE
    _STOPLIST_CACHE = None


def is_noise_person_entity(text: str, label: str) -> bool:
    """Return True if this entity is a common noun/role falsely tagged as PERSON.

    Uses accent-insensitive exact matching. Multi-word names like
    "Fátima Bezerra" are never in the single-word stoplist.

    Args:
        text:  Entity surface form (e.g. "Deputado", "povo", "João Silva").
        label: NER label (e.g. "PER", "PERSON", "LOC").
    """
    if label not in ("PER", "PERSON"):
        return False
    return normalize_token(text) in _get_stoplist()


@dataclass
class CleanEntitiesResult:
    """Result of clean_entities() including quality metrics."""

    entities: list[dict[str, str]]
    noise_filtered_count: int = 0
    person_noise_count: int = 0
    total_person_before: int = 0

    @property
    def person_noise_rate(self) -> float:
        """Fraction of PERSON entities removed as noise (0.0–1.0)."""
        if self.total_person_before == 0:
            return 0.0
        return self.person_noise_count / self.total_person_before


def clean_entities(entities: list[dict[str, str]]) -> CleanEntitiesResult:
    """Remove generic nouns/roles falsely tagged as PERSON.

    Applies accent-insensitive stoplist matching. Non-PERSON entities
    and multi-word proper names are always preserved.

    Args:
        entities: List of {text, label} entity dicts.

    Returns:
        CleanEntitiesResult with the filtered entity list and noise metrics:
        - noise_filtered_count / person_noise_count: entities removed
        - total_person_before: PERSON entities seen before filtering
        - person_noise_rate: fraction of PERSON entities that were noise

    Example:
        >>> result = clean_entities([
        ...     {"text": "povo", "label": "PER"},
        ...     {"text": "Fátima Bezerra", "label": "PER"},
        ...     {"text": "Natal", "label": "LOC"},
        ... ])
        >>> [e["text"] for e in result.entities]
        ['Fátima Bezerra', 'Natal']
        >>> result.person_noise_count
        1
        >>> result.person_noise_rate
        0.5
    """
    total_person = sum(1 for e in entities if e.get("label") in ("PER", "PERSON"))
    person_noise = 0
    cleaned: list[dict[str, str]] = []

    for ent in entities:
        text = ent.get("text", "")
        label = ent.get("label", "")
        if is_noise_person_entity(text, label):
            person_noise += 1
        else:
            cleaned.append(ent)

    return CleanEntitiesResult(
        entities=cleaned,
        noise_filtered_count=person_noise,
        person_noise_count=person_noise,
        total_person_before=total_person,
    )
