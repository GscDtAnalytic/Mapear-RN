"""Self-serve target management — Stage 2C v1 mechanics layer.

Operates on the bundled region seeds in
``mapear-domain/src/mapear_domain/seeds/<region>/targets.csv`` —
the politician watch list that drives identity resolution. Provides
typed add / remove / list / validate operations so future UI / API
layers (Stage 2C v2, Stage 2D) wrap this library instead of
re-implementing CSV parsing + validation.

Why this exists
---------------

Before Stage 2C the team edited ``targets.csv`` by hand: opening the
CSV in a text editor, appending a row, hoping no commas in fields
broke quoting, and praying ``person_id`` didn't already exist. The
risks: duplicate ``person_id`` (silent data merge in dbt), alias
collisions (`'Cadu' → cadu_xavier` AND `'Cadu' → cadu_silva`), orphan
``city`` references on mayor rows, malformed roles. The library catches
all of those before the write lands.

Scope v1
--------

* CLI mechanics over ``targets.csv``. CRUD + integrity validation.
* No UI (CLI only; UI wraps this).
* No tenant overlay — operates on bundled region seeds. The CSV is
  shared across tenants until Stage 2C v2 introduces overlay paths
  ``seeds/<region>/<tenant>/targets.csv`` stacking onto the base.
* No pipeline reprocessing trigger. After an ``add``, the next
  scheduled batch picks up the new target via ``load_region()``;
  historical raw rows are not retroactively re-NER'd. Backfill is a
  separate ops decision.
* RN protection: the CLI refuses to write to the RN region by default
  because the canonical RN targets live in ``dbt/seeds/`` and are
  managed via git PR. ``--allow-rn`` overrides for one-off ops.

The ``test`` region is the primary playground for tests + smoke runs.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from mapear_domain.region import load_region

# Bundled seeds path — same constant as region.py uses.
_SEEDS_DIR = Path(__file__).parent / "seeds"

TargetRole = Literal[
    "governor",
    "vice_governor",
    "governor_candidate",
    "mayor",
    "senator",
    "deputy_federal",
    "deputy_state",
    "councilor",
]

_VALID_ROLES: frozenset[str] = frozenset(
    [
        "governor",
        "vice_governor",
        "governor_candidate",
        "mayor",
        "senator",
        "deputy_federal",
        "deputy_state",
        "councilor",
    ]
)

_TARGETS_CSV_FIELDS: tuple[str, ...] = (
    "person_id",
    "name",
    "aliases",
    "role",
    "city",
    "party",
    "term_start",
    "term_end",
    "is_incumbent",
    "facebook_page",
    "instagram_username",
    "x_handle",
    "tiktok_handle",
    "notes",
)

_PERSON_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class TargetSpec(BaseModel):
    """One politician target — mirrors a row in ``targets.csv``."""

    person_id: str
    name: str
    role: TargetRole
    aliases: list[str] = Field(default_factory=list)
    city: str | None = None
    party: str | None = None
    term_start: int | None = None
    term_end: int | None = None
    is_incumbent: bool = False
    facebook_page: str | None = None
    instagram_username: str | None = None
    x_handle: str | None = None
    tiktok_handle: str | None = None
    notes: str | None = None

    @field_validator("person_id")
    @classmethod
    def _check_person_id(cls, v: str) -> str:
        if not _PERSON_ID_PATTERN.match(v):
            raise ValueError(
                f"person_id={v!r} must match ^[a-z][a-z0-9_]{{2,63}}$ "
                "(lowercase alphanumeric + underscore, starts with letter)"
            )
        return v

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must be a non-empty string")
        return v.strip()

    @field_validator("aliases")
    @classmethod
    def _check_aliases(cls, v: list[str]) -> list[str]:
        return [a.strip() for a in v if a and a.strip()]

    @model_validator(mode="after")
    def _check_role_specific(self) -> TargetSpec:
        if self.role == "mayor" and not self.city:
            raise ValueError("role=mayor requires `city`")
        return self

    @classmethod
    def from_csv_row(cls, row: dict[str, str]) -> TargetSpec:
        """Parse a CSV row dict into a TargetSpec."""
        return cls(
            person_id=row["person_id"],
            name=row["name"],
            role=row["role"],  # type: ignore[arg-type]
            aliases=[a for a in (row.get("aliases") or "").split(";") if a],
            city=row.get("city") or None,
            party=row.get("party") or None,
            term_start=_to_int(row.get("term_start")),
            term_end=_to_int(row.get("term_end")),
            is_incumbent=_to_bool(row.get("is_incumbent")),
            facebook_page=row.get("facebook_page") or None,
            instagram_username=row.get("instagram_username") or None,
            x_handle=row.get("x_handle") or None,
            tiktok_handle=row.get("tiktok_handle") or None,
            notes=row.get("notes") or None,
        )

    def to_csv_row(self) -> dict[str, str]:
        """Render as a CSV row dict suitable for ``csv.DictWriter``."""
        return {
            "person_id": self.person_id,
            "name": self.name,
            "aliases": ";".join(self.aliases),
            "role": self.role,
            "city": self.city or "",
            "party": self.party or "",
            "term_start": str(self.term_start) if self.term_start is not None else "",
            "term_end": str(self.term_end) if self.term_end is not None else "",
            "is_incumbent": "true" if self.is_incumbent else "false",
            "facebook_page": self.facebook_page or "",
            "instagram_username": self.instagram_username or "",
            "x_handle": self.x_handle or "",
            "tiktok_handle": self.tiktok_handle or "",
            "notes": self.notes or "",
        }


def _to_int(s: str | None) -> int | None:
    if not s or not s.strip():
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _to_bool(s: str | None) -> bool:
    if not s:
        return False
    return s.strip().lower() in {"true", "1", "yes", "t"}


# --- File I/O ----------------------------------------------------------------


def _targets_path(region_id: str, seeds_dir: Path | None = None) -> Path:
    base = seeds_dir if seeds_dir is not None else _SEEDS_DIR
    return base / region_id / "targets.csv"


def _read_targets(region_id: str, seeds_dir: Path | None = None) -> list[TargetSpec]:
    path = _targets_path(region_id, seeds_dir)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [TargetSpec.from_csv_row(row) for row in csv.DictReader(f)]


def _write_targets(
    targets: list[TargetSpec], region_id: str, seeds_dir: Path | None = None
) -> None:
    path = _targets_path(region_id, seeds_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(_TARGETS_CSV_FIELDS))
        writer.writeheader()
        for t in targets:
            writer.writerow(t.to_csv_row())


# --- Operations --------------------------------------------------------------


class TargetOperationError(Exception):
    """Raised when an add/remove/validate fails domain rules."""


def _guard_rn(region_id: str, seeds_dir: Path | None, allow_rn: bool) -> None:
    """RN canonical seeds live in dbt/seeds/ under git control. Refuse to
    write directly unless the operator opts in with ``allow_rn=True``."""
    if region_id == "rn" and seeds_dir is None and not allow_rn:
        raise TargetOperationError(
            "Refusing to write to the bundled RN seeds. RN target list is "
            "canonical and managed via dbt/seeds/rn_targets.csv under git. "
            "Use a feature branch + PR, or pass allow_rn=True / --allow-rn "
            "for a one-off ops override (then sync back to dbt/seeds/)."
        )


def add_target(
    spec: TargetSpec,
    region_id: str,
    *,
    seeds_dir: Path | None = None,
    allow_rn: bool = False,
) -> dict[str, Any]:
    """Append a target to the region's targets.csv. Idempotent on identical row."""
    _guard_rn(region_id, seeds_dir, allow_rn)
    existing = _read_targets(region_id, seeds_dir)
    by_pid = {t.person_id: t for t in existing}

    if spec.person_id in by_pid:
        if by_pid[spec.person_id] == spec:
            return {"status": "unchanged", "person_id": spec.person_id}
        raise TargetOperationError(
            f"person_id={spec.person_id!r} already exists with different data. "
            "Use remove_target then add_target, or edit the CSV via PR."
        )

    # Canonical name collision (case-insensitive across all canonical names).
    name_lower = spec.name.lower()
    for t in existing:
        if t.name.lower() == name_lower:
            raise TargetOperationError(
                f"name={spec.name!r} already used by person_id={t.person_id!r}. "
                "Pick a more specific canonical name."
            )

    # Alias collision: a new alias must not already be a canonical name
    # or an alias of a different target.
    canonical_names = {t.name.lower() for t in existing}
    other_aliases = {a.lower(): t.person_id for t in existing for a in t.aliases}
    for alias in spec.aliases:
        a_lower = alias.lower()
        if a_lower in canonical_names:
            raise TargetOperationError(
                f"alias={alias!r} collides with canonical name of another target."
            )
        if a_lower in other_aliases:
            raise TargetOperationError(
                f"alias={alias!r} already aliases person_id="
                f"{other_aliases[a_lower]!r}."
            )

    # FK: mayor must point to an existing city in the region.
    if spec.role == "mayor":
        region = load_region(region_id, seeds_base=seeds_dir)
        cities = {c.lower() for c in region.get_city_names()}
        if spec.city and spec.city.lower() not in cities:
            raise TargetOperationError(
                f"role=mayor with city={spec.city!r} but that city is not in "
                f"region {region_id!r}. Add the city first or fix the spelling."
            )

    _write_targets([*existing, spec], region_id, seeds_dir)
    return {"status": "added", "person_id": spec.person_id, "role": spec.role}


def remove_target(
    person_id: str,
    region_id: str,
    *,
    seeds_dir: Path | None = None,
    allow_rn: bool = False,
) -> dict[str, Any]:
    """Remove a target by person_id. Idempotent on missing target."""
    _guard_rn(region_id, seeds_dir, allow_rn)
    existing = _read_targets(region_id, seeds_dir)
    remaining = [t for t in existing if t.person_id != person_id]
    if len(remaining) == len(existing):
        return {"status": "missing", "person_id": person_id}
    _write_targets(remaining, region_id, seeds_dir)
    return {"status": "removed", "person_id": person_id}


def list_targets(
    region_id: str,
    *,
    role: str | None = None,
    seeds_dir: Path | None = None,
) -> list[TargetSpec]:
    """Return targets, optionally filtered by role."""
    targets = _read_targets(region_id, seeds_dir)
    if role:
        targets = [t for t in targets if t.role == role]
    return targets


def validate_region(region_id: str, *, seeds_dir: Path | None = None) -> dict[str, Any]:
    """Run integrity checks; return a report dict.

    Checks:
      * duplicate ``person_id``
      * canonical name collisions (case-insensitive)
      * alias collisions across targets
      * orphan ``city`` FK on mayor rows
      * unknown role values
    """
    targets = _read_targets(region_id, seeds_dir)
    issues: list[dict[str, str]] = []

    seen_ids: set[str] = set()
    seen_names: dict[str, str] = {}
    seen_aliases: dict[str, str] = {}

    region = load_region(region_id, seeds_base=seeds_dir)
    cities_lower = {c.lower() for c in region.get_city_names()}

    for t in targets:
        if t.person_id in seen_ids:
            issues.append({"kind": "duplicate_person_id", "person_id": t.person_id})
        seen_ids.add(t.person_id)

        name_l = t.name.lower()
        if name_l in seen_names and seen_names[name_l] != t.person_id:
            issues.append(
                {
                    "kind": "name_collision",
                    "name": t.name,
                    "person_ids": f"{seen_names[name_l]},{t.person_id}",
                }
            )
        seen_names[name_l] = t.person_id

        for alias in t.aliases:
            a_l = alias.lower()
            if a_l in seen_names and seen_names[a_l] != t.person_id:
                issues.append(
                    {
                        "kind": "alias_canonical_collision",
                        "alias": alias,
                        "canonical_of": seen_names[a_l],
                        "owned_by": t.person_id,
                    }
                )
            if a_l in seen_aliases and seen_aliases[a_l] != t.person_id:
                issues.append(
                    {
                        "kind": "alias_collision",
                        "alias": alias,
                        "person_ids": f"{seen_aliases[a_l]},{t.person_id}",
                    }
                )
            seen_aliases[a_l] = t.person_id

        if t.role == "mayor" and t.city and t.city.lower() not in cities_lower:
            issues.append(
                {
                    "kind": "orphan_city_fk",
                    "person_id": t.person_id,
                    "city": t.city,
                }
            )

        if t.role not in _VALID_ROLES:
            issues.append(
                {"kind": "unknown_role", "person_id": t.person_id, "role": t.role}
            )

    return {
        "region": region_id,
        "n_targets": len(targets),
        "n_issues": len(issues),
        "issues": issues,
    }


# --- CLI ---------------------------------------------------------------------


def _cli_add(args: argparse.Namespace) -> int:
    aliases = (
        [a.strip() for a in args.aliases.split(";") if a.strip()]
        if args.aliases
        else []
    )
    spec = TargetSpec(
        person_id=args.person_id,
        name=args.name,
        role=args.role,
        aliases=aliases,
        city=args.city,
        party=args.party,
        term_start=args.term_start,
        term_end=args.term_end,
        is_incumbent=args.is_incumbent,
        facebook_page=args.facebook_page,
        instagram_username=args.instagram_username,
        x_handle=args.x_handle,
        tiktok_handle=args.tiktok_handle,
        notes=args.notes,
    )
    result = add_target(spec, args.region, allow_rn=args.allow_rn)
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


def _cli_remove(args: argparse.Namespace) -> int:
    result = remove_target(args.person_id, args.region, allow_rn=args.allow_rn)
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0 if result["status"] != "missing" else 1


def _cli_list(args: argparse.Namespace) -> int:
    targets = list_targets(args.region, role=args.role)
    if args.json:
        payload = json.dumps([t.model_dump() for t in targets], indent=2, default=str)
        sys.stdout.write(payload + "\n")
    else:
        sys.stdout.write(f"{len(targets)} targets in region={args.region}\n")
        for t in targets:
            city = f" ({t.city})" if t.city else ""
            aliases = f" aka {', '.join(t.aliases)}" if t.aliases else ""
            sys.stdout.write(f"  {t.person_id}: {t.name} [{t.role}{city}]{aliases}\n")
    return 0


def _cli_validate(args: argparse.Namespace) -> int:
    report = validate_region(args.region)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0 if report["n_issues"] == 0 else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    add_p = sub.add_parser("add", help="Add a target")
    add_p.add_argument("--region", required=True)
    add_p.add_argument("--person-id", required=True, dest="person_id")
    add_p.add_argument("--name", required=True)
    add_p.add_argument("--role", required=True, choices=sorted(_VALID_ROLES))
    add_p.add_argument("--aliases", default="", help="Semicolon-separated")
    add_p.add_argument("--city", default=None)
    add_p.add_argument("--party", default=None)
    add_p.add_argument("--term-start", type=int, default=None, dest="term_start")
    add_p.add_argument("--term-end", type=int, default=None, dest="term_end")
    add_p.add_argument("--is-incumbent", action="store_true", dest="is_incumbent")
    add_p.add_argument("--facebook-page", default=None, dest="facebook_page")
    add_p.add_argument("--instagram-username", default=None, dest="instagram_username")
    add_p.add_argument("--x-handle", default=None, dest="x_handle")
    add_p.add_argument("--tiktok-handle", default=None, dest="tiktok_handle")
    add_p.add_argument("--notes", default=None)
    add_p.add_argument(
        "--allow-rn",
        action="store_true",
        dest="allow_rn",
        help="Allow writes to bundled RN seeds (default refused — use dbt PR).",
    )
    add_p.set_defaults(func=_cli_add)

    rm_p = sub.add_parser("remove", help="Remove a target by person_id")
    rm_p.add_argument("--region", required=True)
    rm_p.add_argument("--person-id", required=True, dest="person_id")
    rm_p.add_argument("--allow-rn", action="store_true", dest="allow_rn")
    rm_p.set_defaults(func=_cli_remove)

    ls_p = sub.add_parser("list", help="List targets in a region")
    ls_p.add_argument("--region", required=True)
    ls_p.add_argument("--role", default=None, choices=sorted(_VALID_ROLES))
    ls_p.add_argument("--json", action="store_true")
    ls_p.set_defaults(func=_cli_list)

    val_p = sub.add_parser("validate", help="Integrity check on a region")
    val_p.add_argument("--region", required=True)
    val_p.set_defaults(func=_cli_validate)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except TargetOperationError as exc:
        payload = json.dumps({"status": "error", "message": str(exc)}, indent=2)
        sys.stdout.write(payload + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
