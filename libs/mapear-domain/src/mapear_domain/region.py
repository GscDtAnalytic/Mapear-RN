"""Region abstraction — encapsulates all geo/political entities for a monitored region.

Decouples region-specific data (city names, mayor aliases, governor info) from
domain logic. Consumers receive a Region instance rather than importing from the
RN-specific rn_entities module directly.

Usage:
    from mapear_domain.region import load_region

    region = load_region("rn")     # production
    region = load_region("test")   # synthetic data for unit tests

See mapear-domain/REGIONS.md for instructions on adding a new region.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from pydantic import BaseModel, Field

# seeds/ lives inside the package so it's accessible after pip/poetry install
_SEEDS_DIR = Path(__file__).parent / "seeds"


def _find_dbt_seeds_dir() -> Path | None:
    """Locate the monorepo's ``dbt/seeds/`` by walking up from this module.

    The canonical RN region CSVs double as dbt seeds and live in
    ``dbt/seeds/``. Anchoring to the repo layout — instead of the process
    CWD — keeps resolution stable regardless of where a caller runs from
    (the old ``Path("dbt/seeds/...")`` / ``Path("../dbt/seeds/...")`` pair
    silently depended on the CWD being exactly one level under the root).
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "dbt" / "seeds"
        if candidate.is_dir():
            return candidate
    return None


_DBT_SEEDS_DIR = _find_dbt_seeds_dir()


def _rn_fallbacks(filename: str) -> list[Path]:
    """dbt/seeds fallback for the "rn" region's bundled-seed lookup."""
    return [_DBT_SEEDS_DIR / filename] if _DBT_SEEDS_DIR is not None else []


# dbt seed fallbacks for the "rn" region (monorepo layout)
_RN_CSV_FALLBACKS = _rn_fallbacks("rn_cities_mayors.csv")
_RN_GOVERNOR_FALLBACKS = _rn_fallbacks("rn_governor.csv")
_RN_CANDIDATES_FALLBACKS = _rn_fallbacks("rn_governor_candidates.csv")
_RN_TARGETS_FALLBACKS = _rn_fallbacks("rn_targets.csv")


def _resolve_csv(bundled: Path, fallbacks: list[Path]) -> Path:
    """Return bundled path if it exists, otherwise the first existing fallback."""
    if bundled.exists():
        return bundled
    for fb in fallbacks:
        if fb.exists():
            return fb
    return bundled  # caller handles missing file


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


class Politician(BaseModel):
    """A monitored political figure loaded from targets.csv."""

    person_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    role: str
    city: str | None = None
    party: str | None = None
    handles: dict[str, str] = Field(default_factory=dict)  # platform → handle/username
    mandate_start: int | None = None  # year
    mandate_end: int | None = None  # year
    is_incumbent: bool = False


def _politician_from_row(row: dict[str, str]) -> Politician:
    handles: dict[str, str] = {}
    for platform, col in (
        ("facebook", "facebook_page"),
        ("instagram", "instagram_username"),
        ("x", "x_handle"),
        ("tiktok", "tiktok_handle"),
    ):
        if row.get(col):
            handles[platform] = row[
                col
            ].lower()  # normalize at data boundary; platforms are case-insensitive

    aliases = [a.strip() for a in row.get("aliases", "").split(";") if a.strip()]

    def _year(val: str | None) -> int | None:
        return int(val) if val and val.isdigit() else None

    return Politician(
        person_id=row["person_id"],
        name=row["name"],
        aliases=aliases,
        role=row.get("role", ""),
        city=row.get("city") or None,
        party=row.get("party") or None,
        handles=handles,
        mandate_start=_year(row.get("term_start")),
        mandate_end=_year(row.get("term_end")),
        is_incumbent=row.get("is_incumbent", "").lower() == "true",
    )


class Region(BaseModel):
    """All geo-political entities and aliases for a monitored region."""

    id: str

    # Raw rows from the cities/mayors CSV seed
    cities_mayors_rows: list[dict[str, str]] = Field(default_factory=list)
    # Raw rows from the governor seed
    governor_rows: list[dict[str, str]] = Field(default_factory=list)
    # Raw rows from the governor candidates seed
    governor_candidate_rows: list[dict[str, str]] = Field(default_factory=list)

    # Typed politicians loaded from targets.csv
    politicians: list[Politician] = Field(default_factory=list)

    # Alias lookups (lowercase key → canonical form)
    city_aliases: dict[str, str] = Field(default_factory=dict)
    mayor_aliases: dict[str, str] = Field(default_factory=dict)
    governor_aliases: dict[str, str] = Field(default_factory=dict)
    politician_aliases: dict[str, str] = Field(default_factory=dict)

    def get_city_names(self) -> set[str]:
        """City names from CSV plus all canonical alias values."""
        from_csv = {r["city"] for r in self.cities_mayors_rows if r.get("city")}
        return from_csv | set(self.city_aliases.values())

    def get_mayor_names(self) -> set[str]:
        """Mayor names from CSV plus all canonical alias values."""
        from_csv = {r["mayor"] for r in self.cities_mayors_rows if r.get("mayor")}
        return from_csv | set(self.mayor_aliases.values())

    def get_governor_names(self) -> set[str]:
        """Wide governor set: incumbents + aliases + politicians + candidates.

        Weak relevance signal — used to populate ``mentioned_governors``.
        Do not use alone to determine is_rn_relevant; use
        ``get_incumbent_governor_names`` for that.
        """
        incumbents = {g["name"] for g in self.governor_rows if g.get("name")}
        aliases = set(self.governor_aliases.values())
        politicians = set(self.politician_aliases.values())
        candidates = {c["name"] for c in self.governor_candidate_rows if c.get("name")}
        return incumbents | aliases | politicians | candidates

    def get_incumbent_governor_names(self) -> set[str]:
        """Only the sitting governor(s). Strong relevance signal."""
        return {
            g["name"]
            for g in self.governor_rows
            if g.get("role") == "governor" and g.get("name")
        }

    def get_governor_candidate_names(self) -> set[str]:
        """All declared governor candidates from the seed file."""
        return {c["name"] for c in self.governor_candidate_rows if c.get("name")}

    def get_party_names(self) -> set[str]:
        """Party abbreviations from mayors CSV plus governor parties."""
        from_csv = {r["party"] for r in self.cities_mayors_rows if r.get("party")}
        from_gov = {g["party"] for g in self.governor_rows if g.get("party")}
        return from_csv | from_gov

    def normalize_entity(self, name: str) -> str:
        """Return canonical form for a name using alias dictionaries."""
        lower = name.strip().lower()
        for mapping in (
            self.city_aliases,
            self.mayor_aliases,
            self.governor_aliases,
            self.politician_aliases,
        ):
            if lower in mapping:
                return mapping[lower]
        return name.strip()

    def get_politicians(self) -> list[Politician]:
        return self.politicians

    def get_politicians_by_role(self, role: str) -> list[Politician]:
        return [p for p in self.politicians if p.role == role]

    def get_politician_by_handle(self, platform: str, handle: str) -> Politician | None:
        """Return the politician with the given handle on a platform, or None.

        Both platform and handle are normalized to lowercase before comparison;
        handles stored in politicians are always lowercase (normalized at load time).

        Returns None when no politician has that (platform, handle) pair — the
        platform is not tracked, the handle is blank, or it belongs to no known
        politician.
        """
        plat = platform.lower()
        h = handle.lower()
        for p in self.politicians:
            if p.handles.get(plat) == h:
                return p
        return None

    def get_city_for_person_id(self, person_id: str) -> str | None:
        """Return the city associated with a person_id, or None.

        Returns None in three distinct cases:
        - person_id not found in the politicians list
        - politician found but their role has no city (governor, senator, etc.)
        - politician found but the city field was blank in the seed CSV

        To distinguish these cases, iterate ``self.politicians`` directly and
        inspect the ``Politician`` model.
        """
        for p in self.politicians:
            if p.person_id == person_id:
                return p.city
        return None

    def load_seed_data(self) -> list[dict[str, str]]:
        """Compat: same interface as rn_entities.load_seed_data()."""
        return self.cities_mayors_rows


def load_region(region_id: str, seeds_base: Path | None = None) -> Region:
    """Load a Region from its seed directory.

    Args:
        region_id:  One of the region identifiers bundled in mapear-domain/seeds/
                    (e.g. ``"rn"``, ``"test"``).
        seeds_base: Override the seeds root directory. Defaults to the
                    ``seeds/`` directory bundled with mapear-domain.
    """
    base = seeds_base if seeds_base is not None else _SEEDS_DIR
    region_dir = base / region_id

    # Aliases config
    aliases_path = region_dir / "aliases.json"
    aliases: dict = {}
    if aliases_path.exists():
        aliases = json.loads(aliases_path.read_text(encoding="utf-8"))

    # Cities/mayors CSV — prefer bundled, fall back to dbt/seeds for "rn"
    cm_csv = _resolve_csv(
        region_dir / "cities_mayors.csv",
        _RN_CSV_FALLBACKS if region_id == "rn" else [],
    )
    cities_mayors_rows = _read_csv(cm_csv)

    # Governor CSV
    gov_csv = _resolve_csv(
        region_dir / "governor.csv",
        _RN_GOVERNOR_FALLBACKS if region_id == "rn" else [],
    )
    governor_rows = _read_csv(gov_csv)

    # Governor candidates CSV
    cand_csv = _resolve_csv(
        region_dir / "governor_candidates.csv",
        _RN_CANDIDATES_FALLBACKS if region_id == "rn" else [],
    )
    candidate_rows = _read_csv(cand_csv)

    # Targets CSV — typed politicians
    targets_csv = _resolve_csv(
        region_dir / "targets.csv",
        _RN_TARGETS_FALLBACKS if region_id == "rn" else [],
    )
    politicians = [
        _politician_from_row(row)
        for row in _read_csv(targets_csv)
        if row.get("person_id")
    ]

    return Region(
        id=region_id,
        cities_mayors_rows=cities_mayors_rows,
        governor_rows=governor_rows,
        governor_candidate_rows=candidate_rows,
        politicians=politicians,
        city_aliases=aliases.get("city_aliases", {}),
        mayor_aliases=aliases.get("mayor_aliases", {}),
        governor_aliases=aliases.get("governor_aliases", {}),
        politician_aliases=aliases.get("politician_aliases", {}),
    )
