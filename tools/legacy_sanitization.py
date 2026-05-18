#!/usr/bin/env python3
"""Inventory of legacy artifacts slated for removal in the electoral-pivot Fase 2.

This script is the single source of truth for what will be deleted or
refactored once the PersonResolver-based gold layer (`fct_content_gold`,
`dim_persons`) fully supersedes the RN-relevance-only legacy surface
(`fct_content`, `dim_rn_cities_mayors`, `mentioned_*` arrays, etc.).

Modes
-----
--dryrun (default)
    Walk the repo, emit a structured inventory. Performs no mutations;
    safe to run in any environment. Writes a Markdown report to
    ``reports/legacy_sanitization_<YYYYMMDD>.md`` and also prints to
    stdout (use ``--quiet`` to skip stdout).

--apply
    Reserved for Fase 2. Currently raises ``NotImplementedError`` — the
    apply path will be filled in once consumers have migrated off the
    legacy surface and schema drift (bq update) is planned per BL-11.

--format {md,json}
    Output format. Defaults to ``md``; ``json`` emits a machine-readable
    payload on stdout (skips report file).

Entries
-------
Each entry has: kind, path (or symbol), rationale, replacement, and an
estimated risk tier. Risk tiers:

* low    — pure docs, unused scripts, redundant seeds.
* medium — dbt models with active downstream consumers (dashboards,
           exports).
* high   — physical columns that require ``bq update`` and pipeline
           coordination (BL-11 ops flow).

Consumers MUST migrate away from "medium" and "high" items before
running ``--apply`` in Fase 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent

RiskTier = Literal["low", "medium", "high"]
Kind = Literal[
    "dbt_model",
    "dbt_seed",
    "dbt_column",
    "pydantic_field",
    "python_module",
    "script",
]


@dataclass(frozen=True)
class LegacyEntry:
    kind: Kind
    target: str
    rationale: str
    replacement: str
    risk: RiskTier


INVENTORY: tuple[LegacyEntry, ...] = (
    # --- dbt models ---
    LegacyEntry(
        kind="dbt_model",
        target="dbt/models/marts/fct_content.sql",
        rationale=(
            "Legacy fact table gated only by is_rn_relevant. Lets homônimos "
            "and national noise reach analytics when NER guesses wrong "
            "(BL-12 Judas Tadeu pattern). Superseded by the PersonResolver "
            "gate in fct_content_gold."
        ),
        replacement="dbt/models/marts/fct_content_gold.sql",
        risk="medium",
    ),
    LegacyEntry(
        kind="dbt_model",
        target="dbt/models/marts/dim_rn_cities_mayors.sql",
        rationale=(
            "Composite city+mayor SCD2 built from rn_cities_mayors.csv. "
            "Persons are now first-class via dim_persons (SCD2 on "
            "person_id). Cities stay as a separate lookup (no mayor "
            "denorm) once fct_content_gold replaces fct_content."
        ),
        replacement="dbt/models/marts/dim_persons.sql (+ future dim_rn_cities)",
        risk="medium",
    ),
    # REMOVED 2026-05-06 (Sprint 3, B7) — see docs/sprint3_b7_investigation.md
    LegacyEntry(
        kind="dbt_model",
        target="dbt/models/intermediate/int_articles__rn_enriched.sql",
        rationale=(
            "[REMOVED 2026-05-06 — Sprint 3 B7] Intermediate that joined silver "
            "content to dim_rn_cities_mayors via mentioned_cities[]. Zero downstream "
            "consumers confirmed; logic replicable inline via UNNEST + JOIN. "
            "Fct_content_gold joins directly to dim_persons by person_id, so this "
            "cross-join was dead code in Fase 2."
        ),
        replacement="N/A — absorbed by fct_content_gold joins (executed Sprint 3 B7)",
        risk="low",
    ),
    LegacyEntry(
        kind="dbt_model",
        target="dbt/models/marts/fct_entity_sentiment.sql",
        rationale=(
            "Sentiment unnested from gold_articles.sentiment_by_entity "
            "JSON. Fase 2 replaces the 'entity' string with person_id (FK "
            "to dim_persons). Rewrite needed, not deletion."
        ),
        replacement=(
            "fct_entity_sentiment_v2 (keyed by person_id, refactor in Fase 2)"
        ),
        risk="medium",
    ),
    LegacyEntry(
        kind="dbt_model",
        target="dbt/models/marts/fct_trends.sql",
        rationale=(
            "Aggregates entity sentiment across sources using the legacy "
            "entity-string column. Needs to be re-keyed on person_id "
            "alongside fct_entity_sentiment_v2."
        ),
        replacement="fct_trends_v2 (keyed by person_id, Fase 2)",
        risk="medium",
    ),
    # --- dbt seeds ---
    LegacyEntry(
        kind="dbt_seed",
        target="dbt/seeds/rn_governor.csv",
        rationale=(
            "Single-governor roster. Now fully covered by role='governor' "
            "entries in rn_targets.csv."
        ),
        replacement="dbt/seeds/rn_targets.csv",
        risk="low",
    ),
    LegacyEntry(
        kind="dbt_seed",
        target="dbt/seeds/rn_governor_candidates.csv",
        rationale=(
            "Governor candidate roster. Now fully covered by "
            "role='governor_candidate' entries in rn_targets.csv."
        ),
        replacement="dbt/seeds/rn_targets.csv",
        risk="low",
    ),
    # rn_cities_mayors.csv stays — it's still the lookup for city-level
    # attributes (population, state). Only the mayor denorm in it becomes
    # redundant once dim_persons is the authority.
    # --- Pydantic fields ---
    LegacyEntry(
        kind="pydantic_field",
        target="mapear_core.models.base.SilverArticle.mentioned_mayors",
        rationale=(
            "Array of mayor name strings from NER. Replaced by the "
            "single canonical person_id on SilverArticle (populated by "
            "PersonResolver during silver enrichment)."
        ),
        replacement="SilverArticle.person_id",
        risk="high",
    ),
    LegacyEntry(
        kind="pydantic_field",
        target="mapear_core.models.base.SilverArticle.mentioned_governors",
        rationale=(
            "Same as mentioned_mayors — subsumed by person_id + role='governor' "
            "in dim_persons."
        ),
        replacement="SilverArticle.person_id + dim_persons.role",
        risk="high",
    ),
    LegacyEntry(
        kind="pydantic_field",
        target="mapear_core.models.base.SilverArticle.mentioned_parties",
        rationale=(
            "Party string array. Subsumed by dim_persons.party (via "
            "person_id lookup)."
        ),
        replacement="dim_persons.party (via person_id join)",
        risk="high",
    ),
    LegacyEntry(
        kind="pydantic_field",
        target="mapear_core.models.base.GoldArticle.mentioned_mayors",
        rationale="Same rationale as SilverArticle.mentioned_mayors.",
        replacement="GoldArticle.person_id",
        risk="high",
    ),
    LegacyEntry(
        kind="pydantic_field",
        target="mapear_core.models.base.GoldArticle.mentioned_governors",
        rationale="Same rationale as SilverArticle.mentioned_governors.",
        replacement="GoldArticle.person_id",
        risk="high",
    ),
    LegacyEntry(
        kind="pydantic_field",
        target="mapear_core.models.base.GoldArticle.mentioned_parties",
        rationale="Same rationale as SilverArticle.mentioned_parties.",
        replacement="GoldArticle.person_id + dim_persons.party",
        risk="high",
    ),
    # --- Python modules ---
    LegacyEntry(
        kind="python_module",
        target="mapear_core.rn_entities",
        rationale=(
            "Legacy alias/entity dictionary backed by rn_cities_mayors.csv. "
            "Replaced by PersonResolver, which consolidates name/alias/"
            "handle matching over rn_targets.csv and emits confidence + "
            "scope_status in a single call."
        ),
        replacement="mapear_core.entity_resolution.PersonResolver",
        risk="medium",
    ),
    # --- Scripts / ops ---
    LegacyEntry(
        kind="script",
        target="scripts/export_pilot.py",
        rationale=(
            "Pilot JSON exporter queries fct_content / fct_trends / "
            "dim_rn_cities_mayors / fct_entity_sentiment with the legacy "
            "mentioned_* projection. Must be repointed to fct_content_gold "
            "and dim_persons once downstream consumers accept person_id."
        ),
        replacement="scripts/export_pilot.py (refactor, not delete)",
        risk="medium",
    ),
)


def _verify_paths(entries: tuple[LegacyEntry, ...]) -> list[str]:
    """Warn on entries whose dbt/seed/script target no longer exists."""
    warnings: list[str] = []
    for e in entries:
        if e.kind in ("dbt_model", "dbt_seed", "script"):
            if not (REPO_ROOT / e.target).exists():
                warnings.append(
                    f"[stale] {e.kind} target not found on disk: {e.target}"
                )
    return warnings


def _render_markdown(entries: tuple[LegacyEntry, ...], warnings: list[str]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    lines.append("# Legacy Sanitization Inventory")
    lines.append("")
    lines.append(f"_Generated: {generated_at} — Fase 1 dryrun._")
    lines.append("")
    lines.append(
        "Catalogs artifacts that `--apply` will remove/refactor in Fase 2. "
        "Every entry here has a concrete replacement on the electoral-pivot "
        "surface (PersonResolver, `dim_persons`, `fct_content_gold`)."
    )
    lines.append("")
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")
    by_kind: dict[Kind, list[LegacyEntry]] = {}
    for e in entries:
        by_kind.setdefault(e.kind, []).append(e)
    for kind, items in by_kind.items():
        lines.append(f"## {kind} ({len(items)})")
        lines.append("")
        lines.append("| Target | Risk | Replacement | Rationale |")
        lines.append("| --- | --- | --- | --- |")
        for e in items:
            lines.append(
                f"| `{e.target}` | {e.risk} | `{e.replacement}` | "
                f"{e.rationale.replace(chr(10), ' ')} |"
            )
        lines.append("")
    lines.append("## Risk Summary")
    lines.append("")
    counts: dict[RiskTier, int] = {"low": 0, "medium": 0, "high": 0}
    for e in entries:
        counts[e.risk] += 1
    lines.append(f"- low: {counts['low']}")
    lines.append(f"- medium: {counts['medium']}")
    lines.append(f"- high: {counts['high']}")
    lines.append("")
    return "\n".join(lines)


def _render_json(entries: tuple[LegacyEntry, ...], warnings: list[str]) -> str:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dryrun",
        "warnings": warnings,
        "entries": [asdict(e) for e in entries],
        "counts": {
            "total": len(entries),
            "by_risk": {
                tier: sum(1 for e in entries if e.risk == tier)
                for tier in ("low", "medium", "high")
            },
            "by_kind": {
                kind: sum(1 for e in entries if e.kind == kind)
                for kind in sorted({e.kind for e in entries})
            },
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def run_dryrun(fmt: str, quiet: bool) -> Path | None:
    warnings = _verify_paths(INVENTORY)
    if fmt == "json":
        sys.stdout.write(_render_json(INVENTORY, warnings))
        sys.stdout.write("\n")
        return None
    rendered = _render_markdown(INVENTORY, warnings)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    report_dir = REPO_ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"legacy_sanitization_{today}.md"
    report_path.write_text(rendered, encoding="utf-8")
    if not quiet:
        sys.stdout.write(rendered)
        sys.stdout.write(f"\n\nReport written to: {report_path}\n")
    return report_path


def run_apply() -> None:
    raise NotImplementedError(
        "apply mode is reserved for Fase 2. Fase 1 scope is dryrun-only — "
        "the inventory here is the contract that Fase 2 must honor. "
        "Track the migration gate in the pivot plan before enabling this."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory of legacy artifacts for the electoral pivot."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Reserved for Fase 2 — currently raises NotImplementedError.",
    )
    parser.add_argument(
        "--format",
        choices=("md", "json"),
        default="md",
        help="Output format (default: md).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout when writing the markdown report.",
    )
    args = parser.parse_args()

    if args.apply:
        run_apply()
        return 0
    run_dryrun(args.format, args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
