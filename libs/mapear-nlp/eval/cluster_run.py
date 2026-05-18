"""Narrative clustering eval — Eixo 2 v2a.

For each gold case, runs ``compute_narrative_clusters`` over a bundled
list of synthetic embeddings and checks the detected cluster structure
against the labelled ``expected_clusters_json``. Scoring uses **strict
exact-match** per case: the case passes iff every expected cluster
appears in the detector output as an exact member-set and no extra
non-outlier clusters are emitted.

The strictness keeps the gate honest at v2a — fuzzy matching (Jaccard
between detected and expected sets) hides regressions where the
algorithm drifts toward over-merging or over-splitting. v3 will add
soft metrics (Adjusted Rand Index, NMI) once the gold set grows beyond
the synthetic-embedding tier.

Floor: 80% of cases must pass. Default thresholds (cosine_threshold
algorithm, threshold=0.75, min_size=3) hit 12/12 on the bundled set.

Why cosine_threshold and not HDBSCAN at the gate
------------------------------------------------
The cosine_threshold path is pure-Python and runs in CI without the
``embeddings`` dep group (sentence-transformers, hdbscan,
scikit-learn). HDBSCAN-on-real-text evaluation lives in a separate
opt-in harness once the ``embeddings`` group is installed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from mapear_infra.config import get_settings

from mapear_nlp.clustering.narrative import compute_narrative_clusters

_GOLD_SET = Path(__file__).resolve().parent / "narrative_cluster_gold_set.csv"
_PASS_FLOOR = 0.80


def _load_cases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _expected_clusters(case: dict) -> set[frozenset[str]]:
    raw = json.loads(case["expected_clusters_json"])
    return {frozenset(members) for members in raw}


def _detected_clusters(
    case: dict,
    *,
    algorithm: str,
    min_size: int,
    cosine_threshold: float,
) -> set[frozenset[str]]:
    items = [(h, vec) for h, vec in json.loads(case["embeddings_json"])]
    result = compute_narrative_clusters(
        items,
        algorithm=algorithm,  # type: ignore[arg-type]
        min_size=min_size,
        cosine_threshold=cosine_threshold,
    )
    return {frozenset(c.members) for c in result.clusters}


def run(gold_path: Path = _GOLD_SET) -> int:
    settings = get_settings()
    cases = _load_cases(gold_path)
    if not cases:
        sys.stderr.write("ERROR: gold set is empty\n")
        return 2

    # The CI gate runs cosine_threshold (pure-Python). Settings still
    # drives min_size + threshold so prod tweaks are reflected here.
    algorithm = "cosine_threshold"
    passed = 0
    failed = 0
    for case in cases:
        expected = _expected_clusters(case)
        detected = _detected_clusters(
            case,
            algorithm=algorithm,
            min_size=settings.embeddings.cluster_min_size,
            cosine_threshold=settings.embeddings.cluster_cosine_threshold,
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
        f"\nNarrative cluster eval ({algorithm}): {passed}/{total} cases passed "
        f"(pass_rate={pass_rate:.3f}, floor={_PASS_FLOOR})\n"
    )
    if pass_rate < _PASS_FLOOR:
        sys.stderr.write("GATE FAIL\n")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Narrative clustering eval — Eixo 2 v2a"
    )
    parser.add_argument("--gold", type=Path, default=_GOLD_SET)
    args = parser.parse_args()
    return run(args.gold)


if __name__ == "__main__":
    sys.exit(main())
