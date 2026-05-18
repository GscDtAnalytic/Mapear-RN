"""Discovery-layer region keyword filter.

Pre-filter RSS feed entries so only those mentioning monitored cities,
mayors, governors, or the state sigla get enqueued for full extraction
+ NER. Folha/Estadão/Globo generate ~85% of volume but <1% of region
signal; dropping non-matching entries at discovery time cuts downstream
scraping and NER cost in half without losing coverage (BL-08 / FASE 2).

Feeds flagged ``is_rn_focused`` in the ``feed_sources`` table bypass
this filter — their entire output is assumed region-relevant.

Stage 2A: ``matches`` now accepts an optional ``region`` parameter.
When omitted, it loads the region named by ``settings.mapear_region``,
which keeps every legacy ``rn_filter.matches(...)`` call site working
without modification. The keyword index is cached per ``region.id`` so
multi-region pipelines stay isolated.
"""

import re
import unicodedata

from mapear_domain.region import Region

# Bare state sigla. Case-sensitive + word-boundary to avoid matching
# "rn" in URL paths / "porno" / "modern". Still matches "PL-RN", "no RN",
# etc. — those are acceptable FPs at discovery time (downstream NER
# filters real relevance). TECH-DEBT (Stage 2A): sigla is RN-specific;
# future Region.state_sigla field will unblock multi-region.
_RN_SIGLA = re.compile(r"\bRN\b")

# Per-region cached keyword index. Indexed by Region.id — small set
# (typically 1 entry per pipeline run), no eviction needed.
_KEYWORD_INDEX_CACHE: dict[str, frozenset[str]] = {}


def _strip_accents(text: str) -> str:
    """Lowercase + strip diacritics for accent-insensitive matching."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _keyword_index(region: Region) -> frozenset[str]:
    """Return normalized keywords (cities, mayors, governors) for the region.

    Entries like single tokens ("Natal", "Fátima") stay as-is. Multi-token
    entries ("Rio Grande do Norte", "Paulinho Freire") are also kept — we
    check presence in normalized text with `in`, so multi-word matching
    just works.

    Cached per region.id; safe to call hot-path.
    """
    cached = _KEYWORD_INDEX_CACHE.get(region.id)
    if cached is not None:
        return cached

    names: set[str] = set()
    names.update(region.get_city_names())
    names.update(region.get_mayor_names())
    names.update(region.get_governor_names())
    names.update(region.get_incumbent_governor_names())

    # Drop empties and very short tokens that cause FPs ("Sá").
    # "RN" sigla is handled separately by the case-sensitive regex.
    normalized = frozenset(
        _strip_accents(n) for n in names if n and len(n.strip()) >= 4
    )
    _KEYWORD_INDEX_CACHE[region.id] = normalized
    return normalized


def _default_region() -> Region:
    """Resolve the fallback region for legacy callers of ``matches()``.

    Goes through ``mapear_domain.rn_entities._get_region`` because that
    module honours the ``set_region`` test-injection hook the legacy
    conftest fixtures rely on. New code should pass ``region=`` to
    ``matches()`` directly and bypass this fallback.
    """
    from mapear_domain.rn_entities import _get_region

    return _get_region()


def matches(*texts: str | None, region: Region | None = None) -> bool:
    """Return True if any of ``texts`` mentions a region entity or the sigla.

    Args:
        *texts: Arbitrary number of optional strings (title, description,
            summary, etc.). ``None`` and empty strings are ignored.
        region: Region to match against. When omitted, falls back to the
            region active in ``mapear_domain.rn_entities`` — preserves
            the legacy call shape (including ``set_region()`` test
            injection) for code that has not been ported yet.
    """
    if region is None:
        region = _default_region()

    joined_raw = " ".join(t for t in texts if t)
    if not joined_raw:
        return False

    if _RN_SIGLA.search(joined_raw):
        return True

    haystack = _strip_accents(joined_raw)
    return any(keyword in haystack for keyword in _keyword_index(region))


def _keyword_index_clear() -> None:
    """Reset the per-region keyword cache. Used by test fixtures."""
    _KEYWORD_INDEX_CACHE.clear()


# Back-compat shim: pre-Stage-2A code referenced
# ``_keyword_index.cache_clear()`` because the function was decorated
# with ``@lru_cache``. The cache is now a plain dict, but the call site
# still works via this attribute.
_keyword_index.cache_clear = _keyword_index_clear  # type: ignore[attr-defined]
