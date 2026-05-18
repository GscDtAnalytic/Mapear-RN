"""Coactivation eval — Eixo 3 v1.

Runs the v1 classifier (``compute_coactivation_scores``) against a
hand-labelled gold set of 20 author pairs and reports precision +
recall against the ``coordinated`` label.

The classifier decision rule for the eval is simple and matches the
production thresholds: a pair is "predicted coordinated" iff it appears
in the output of ``compute_coactivation_scores`` with
``min_overlap=MAPEAR_CIB_MIN_OVERLAP`` and
``window_hours=MAPEAR_CIB_WINDOW_HOURS`` (overridable per-case in the
gold CSV). ``uncoordinated`` and ``unrelated`` ground-truth labels are
both treated as negatives — the v1 classifier does not need to
distinguish them.

Usage:
    poetry run python -m eval.coactivation_run
    poetry run python -m eval.coactivation_run --gold path/to/custom.csv

Exits non-zero if precision < 0.80 or recall < 0.80 on the bundled
gold set. The threshold is intentionally lo-fi for v1 — the eval is a
regression gate, not a high-bar benchmark.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from mapear_infra.config import get_settings

from mapear_nlp.graph.coactivation import compute_coactivation_scores

_GOLD_SET = Path(__file__).resolve().parent / "coactivation_gold_set.csv"

# Floor thresholds for the regression gate. The eval target is to keep
# the classifier from regressing — v2 will raise the floor as the
# scoring gets stronger.
_PRECISION_FLOOR = 0.80
_RECALL_FLOOR = 0.80


def _parse_iso(ts: str) -> datetime:
    # datetime.fromisoformat handles "...Z" only on 3.11+. Strip and add UTC.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _load_cases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _predict_coordinated(case: dict, default_window: float, default_min: int) -> bool:
    """Return True iff the v1 classifier would flag this pair."""
    activations = [
        {**row, "published_at": _parse_iso(row["published_at"])}
        for row in json.loads(case["activations_json"])
    ]
    window_hours = float(case.get("window_hours") or default_window)
    min_overlap = int(case.get("min_overlap") or default_min)
    pairs = compute_coactivation_scores(
        activations,
        window_hours=window_hours,
        min_overlap=min_overlap,
    )
    target = {
        (
            (case["author_a_platform"], case["author_a_id"]),
            (case["author_b_platform"], case["author_b_id"]),
        ),
        (
            (case["author_b_platform"], case["author_b_id"]),
            (case["author_a_platform"], case["author_a_id"]),
        ),
    }
    for p in pairs:
        a = (p.author_a.platform, p.author_a.author_id)
        b = (p.author_b.platform, p.author_b.author_id)
        if (a, b) in target or (b, a) in target:
            return True
    return False


def run(gold_path: Path = _GOLD_SET) -> int:
    settings = get_settings()
    cases = _load_cases(gold_path)
    if not cases:
        sys.stderr.write("ERROR: gold set is empty\n")
        return 2

    tp = fp = tn = fn = 0
    rows_out: list[str] = []
    for case in cases:
        expected_coord = case["label"] == "coordinated"
        predicted_coord = _predict_coordinated(
            case,
            default_window=settings.cib.window_hours,
            default_min=settings.cib.min_overlap,
        )
        verdict: str
        if expected_coord and predicted_coord:
            tp += 1
            verdict = "TP"
        elif expected_coord and not predicted_coord:
            fn += 1
            verdict = "FN"
        elif not expected_coord and predicted_coord:
            fp += 1
            verdict = "FP"
        else:
            tn += 1
            verdict = "TN"
        rows_out.append(f"{verdict} {case['case_id']:32s} label={case['label']}")

    for line in rows_out:
        sys.stdout.write(line + "\n")

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    )

    sys.stdout.write(
        f"\nCoactivation eval: TP={tp} FP={fp} TN={tn} FN={fn}  "
        f"precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}\n"
    )

    failed = precision < _PRECISION_FLOOR or recall < _RECALL_FLOOR
    if failed:
        sys.stderr.write(
            f"\nGATE FAIL: precision_floor={_PRECISION_FLOOR} "
            f"recall_floor={_RECALL_FLOOR}\n"
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Coactivation eval — Eixo 3 v1")
    parser.add_argument(
        "--gold",
        type=Path,
        default=_GOLD_SET,
        help="Path to coactivation_gold_set.csv (default: bundled)",
    )
    args = parser.parse_args()
    return run(args.gold)


if __name__ == "__main__":
    sys.exit(main())
