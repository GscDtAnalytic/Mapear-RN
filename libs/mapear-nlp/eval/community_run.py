"""Community detection eval — Eixo 3 v2a.

For each gold case, runs co-activation + community detection and
checks that the detected community structure matches the labelled
expected_clusters. Scoring uses **strict exact-match** per case: the
case passes iff every expected cluster appears in the detector output
as an exact member-set and no extra clusters are emitted.

The strictness keeps the gate honest at v2a — fuzzy matching (Jaccard
between detected and expected sets) hides regressions where the
algorithm drifts toward over-merging or over-splitting. v3 will add
soft metrics (Adjusted Rand Index) once the gold set is large enough
to warrant statistical tooling.

Floor: 80% of cases must pass. Default thresholds (Louvain seed=42,
window=24h, min_overlap=3, min_size=3) hit 10/10 on the bundled set.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from mapear_infra.config import get_settings

from mapear_nlp.graph.coactivation import AuthorKey, compute_coactivation_scores
from mapear_nlp.graph.community import build_graph, detect_communities

_GOLD_SET = Path(__file__).resolve().parent / "community_gold_set.csv"
_PASS_FLOOR = 0.80


def _parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _load_cases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _expected_clusters(case: dict) -> set[frozenset[AuthorKey]]:
    raw = json.loads(case["expected_clusters_json"])
    return {
        frozenset(AuthorKey(platform=p, author_id=a) for p, a in members)
        for members in raw
    }


def _detected_clusters(
    case: dict,
    *,
    window_hours: float,
    min_overlap: int,
    algorithm: str,
    resolution: float,
    seed: int,
    min_size: int,
) -> set[frozenset[AuthorKey]]:
    activations = [
        {**row, "published_at": _parse_iso(row["published_at"])}
        for row in json.loads(case["activations_json"])
    ]
    pairs = compute_coactivation_scores(
        activations, window_hours=window_hours, min_overlap=min_overlap
    )
    if not pairs:
        return set()
    graph = build_graph(pairs)
    communities = detect_communities(
        graph,
        algorithm=algorithm,  # type: ignore[arg-type]
        resolution=resolution,
        seed=seed,
        min_size=min_size,
    )
    return {frozenset(c.members) for c in communities}


def run(gold_path: Path = _GOLD_SET) -> int:
    settings = get_settings()
    cases = _load_cases(gold_path)
    if not cases:
        sys.stderr.write("ERROR: gold set is empty\n")
        return 2

    passed = 0
    failed = 0
    for case in cases:
        expected = _expected_clusters(case)
        detected = _detected_clusters(
            case,
            window_hours=settings.cib.window_hours,
            min_overlap=settings.cib.min_overlap,
            algorithm=settings.cib.community_algorithm,
            resolution=settings.cib.community_resolution,
            seed=settings.cib.community_seed,
            min_size=settings.cib.community_min_size,
        )
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
        f"\nCommunity eval: {passed}/{total} cases passed "
        f"(pass_rate={pass_rate:.3f}, floor={_PASS_FLOOR})\n"
    )
    if pass_rate < _PASS_FLOOR:
        sys.stderr.write("GATE FAIL\n")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Community detection eval — Eixo 3 v2a"
    )
    parser.add_argument("--gold", type=Path, default=_GOLD_SET)
    args = parser.parse_args()
    return run(args.gold)


if __name__ == "__main__":
    sys.exit(main())
