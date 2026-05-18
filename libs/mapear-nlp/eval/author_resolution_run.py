"""Author resolution eval — Eixo 3 v2b.

For each gold case, runs ``resolve_personas`` over the labelled
``authors_json`` and checks that the emitted persona memberships
match ``expected_personas_json`` exactly (strict member-set equality
per persona, no extra personas).

Strict matching keeps the gate honest at v2b — fuzzy persona overlap
hides regressions where the resolver drifts toward over-merging or
over-splitting. v3 will add soft metrics (e.g. cluster purity,
B-cubed F1) once the gold set is large enough to warrant statistical
tooling.

Floor: 80% of cases must pass. Default thresholds
(``MAPEAR_CIB_ER_*``) hit 15/15 on the bundled set.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from mapear_domain.entity_resolution.author_resolver import (
    AuthorKey,
    Thresholds,
    resolve_personas,
)
from mapear_infra.config import get_settings

_GOLD_SET = Path(__file__).resolve().parent / "author_resolution_gold_set.csv"
_PASS_FLOOR = 0.80


def _load_cases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _expected_personas(case: dict) -> set[frozenset[AuthorKey]]:
    raw = json.loads(case["expected_personas_json"])
    return {
        frozenset(AuthorKey(platform=p, author_id=a) for p, a in members)
        for members in raw
    }


def _detected_personas(
    case: dict,
    *,
    thresholds: Thresholds,
) -> set[frozenset[AuthorKey]]:
    authors = json.loads(case["authors_json"])
    personas = resolve_personas(authors, thresholds=thresholds)
    return {frozenset(p.members) for p in personas}


def run(gold_path: Path = _GOLD_SET) -> int:
    settings = get_settings()
    thresholds = Thresholds(
        handle_similarity=settings.cib.er_handle_similarity,
        display_name_similarity=settings.cib.er_display_name_similarity,
        min_shared_content=settings.cib.er_min_shared_content,
        use_content_hash_bridge=settings.cib.er_use_content_hash_bridge,
    )
    cases = _load_cases(gold_path)
    if not cases:
        sys.stderr.write("ERROR: gold set is empty\n")
        return 2

    passed = 0
    failed = 0
    for case in cases:
        expected = _expected_personas(case)
        detected = _detected_personas(case, thresholds=thresholds)
        if detected == expected:
            sys.stdout.write(f"PASS {case['case_id']}\n")
            passed += 1
        else:
            failed += 1
            sys.stdout.write(
                f"FAIL {case['case_id']}\n"
                f"  expected: {sorted(map(sorted, expected))}\n"
                f"  detected: {sorted(map(sorted, detected))}\n"
            )

    total = passed + failed
    pass_rate = passed / total if total else 0.0
    sys.stdout.write(
        f"\nAuthor-resolution eval: {passed}/{total} cases passed "
        f"(pass_rate={pass_rate:.3f}, floor={_PASS_FLOOR})\n"
    )
    if pass_rate < _PASS_FLOOR:
        sys.stderr.write("GATE FAIL\n")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Author resolution eval — Eixo 3 v2b")
    parser.add_argument("--gold", type=Path, default=_GOLD_SET)
    args = parser.parse_args()
    return run(args.gold)


if __name__ == "__main__":
    sys.exit(main())
