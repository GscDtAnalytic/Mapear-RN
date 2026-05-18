"""RN-specific entity functions — thin wrappers around Region("rn").

.. deprecated:: Stage 2A (2026-05-10)
    All production pipelines (RSS, social, NLP NER, RSS rn_filter) now
    accept a ``Region`` via dependency injection driven by
    ``settings.mapear_region``. New code should obtain a Region instance
    via ``load_region(settings.mapear_region)`` and call methods on it
    directly.

    What still lives here on purpose:
      * ``set_region`` / ``set_seed_path`` / ``set_candidates_seed_path``
        — test-injection hooks used by ``Mapear-RSS/tests/conftest.py``
        and ad-hoc scripts.
      * ``_get_region`` — the back-compat resolution helper that
        ``rn_filter._default_region`` falls back to so legacy callers
        without a region kwarg still hit the right Region instance.

    Module-level DeprecationWarning is intentionally NOT emitted to
    avoid spamming the legitimate fallback paths. The plain functions
    (``get_city_names`` etc.) have no remaining production import
    sites; removal is queued for a future stage once tests migrate off
    ``set_region`` in favour of explicit Region fixtures.
"""

from pathlib import Path

from mapear_domain.region import Region, load_region

# Module-level state for backward compat (set_seed_path / set_candidates_seed_path)
_override_seed_path: Path | None = None
_override_candidates_seed_path: Path | None = None
_cached_region: Region | None = None


def set_region(region: "Region | None") -> None:
    """Inject a pre-built Region directly (for testing).

    Bypasses CSV loading entirely. Pass None to reset to the default behaviour
    (load from seeds/rn/ on next access). Does not affect _override_seed_path.
    """
    global _cached_region
    _cached_region = region


def set_seed_path(path: Path | None) -> None:
    """Override the cities/mayors CSV path used by default RN functions.

    Invalidates the cached Region so the next call reloads with the new path.
    """
    global _override_seed_path, _cached_region
    _override_seed_path = path
    _cached_region = None


def set_candidates_seed_path(path: Path | None) -> None:
    """Override the governor candidates CSV path."""
    global _override_candidates_seed_path, _cached_region
    _override_candidates_seed_path = path
    _cached_region = None


def _get_region() -> Region:
    """Return the cached RN Region, rebuilding if an override was set."""
    global _cached_region
    if _cached_region is not None:
        return _cached_region

    if _override_seed_path is not None or _override_candidates_seed_path is not None:
        # Build a custom region using the standard "rn" aliases but overridden CSVs
        import json

        from mapear_domain.region import (
            _RN_CANDIDATES_FALLBACKS,
            _RN_GOVERNOR_FALLBACKS,
            _SEEDS_DIR,
            _read_csv,
            _resolve_csv,
        )

        aliases_path = _SEEDS_DIR / "rn" / "aliases.json"
        aliases: dict = {}
        if aliases_path.exists():
            aliases = json.loads(aliases_path.read_text(encoding="utf-8"))

        # Cities/mayors: use the override if set
        if _override_seed_path is not None:
            cm_rows = _read_csv(_override_seed_path)
        else:
            cm_csv = _resolve_csv(_SEEDS_DIR / "rn" / "cities_mayors.csv", [])
            cm_rows = _read_csv(cm_csv)

        gov_csv = _resolve_csv(
            _SEEDS_DIR / "rn" / "governor.csv", _RN_GOVERNOR_FALLBACKS
        )
        gov_rows = _read_csv(gov_csv)

        # Candidates: use the override if set
        if _override_candidates_seed_path is not None:
            cand_rows = _read_csv(_override_candidates_seed_path)
        else:
            cand_csv = _resolve_csv(
                _SEEDS_DIR / "rn" / "governor_candidates.csv",
                _RN_CANDIDATES_FALLBACKS,
            )
            cand_rows = _read_csv(cand_csv)

        _cached_region = Region(
            id="rn",
            cities_mayors_rows=cm_rows,
            governor_rows=gov_rows,
            governor_candidate_rows=cand_rows,
            city_aliases=aliases.get("city_aliases", {}),
            mayor_aliases=aliases.get("mayor_aliases", {}),
            governor_aliases=aliases.get("governor_aliases", {}),
            politician_aliases=aliases.get("politician_aliases", {}),
        )
    else:
        _cached_region = load_region("rn")

    return _cached_region


def load_seed_data() -> list[dict[str, str]]:
    """Load city/mayor data from the dbt seed CSV."""
    return _get_region().load_seed_data()


def get_city_names() -> set[str]:
    """Return set of all monitored city names (including aliases)."""
    return _get_region().get_city_names()


def get_mayor_names() -> set[str]:
    """Return set of all monitored mayor names (including aliases)."""
    return _get_region().get_mayor_names()


def get_governor_names() -> set[str]:
    """Return set of all governor-like names (wide, weak signal).

    Inclui o titular, ex-governadores, candidatos e demais figuras
    políticas RN no plano federal.
    """
    return _get_region().get_governor_names()


def get_incumbent_governor_names() -> set[str]:
    """Return only the incumbent governor canonical name(s).

    Sinal forte para ``is_rn_relevant``.
    """
    return _get_region().get_incumbent_governor_names()


def load_candidates_data() -> list[dict[str, str]]:
    """Load governor candidates data from the seed CSV."""
    return _get_region().governor_candidate_rows


def get_governor_candidate_names() -> set[str]:
    """Return set of all governor candidate names from the seed file."""
    return _get_region().get_governor_candidate_names()


def get_party_names() -> set[str]:
    """Return set of all monitored party abbreviations."""
    return _get_region().get_party_names()


def normalize_entity(name: str) -> str:
    """Normalize an entity name using alias dictionaries."""
    return _get_region().normalize_entity(name)
