"""Eval harness for the political sentiment overlay.

Loads ``gold_set.csv`` (synthetic numeric inputs + expected label per
case), runs the current ``PoliticalSentimentClassifier`` against every
case, and reports F1 macro, per-class precision/recall, and a 3x3
confusion matrix.

Two modes:

* default — compare current metrics against ``baseline.json``. Exit non-
  zero if F1 macro dropped by more than ``--max-f1-drop`` (default 0.05)
  or if any case in the baseline regressed (predicted label changed away
  from the previous prediction). The CI gate runs in this mode.

* ``--update-baseline`` — overwrite ``baseline.json`` with the current
  metrics + per-case predictions. Run this after a deliberate change to
  the rules / thresholds, and commit the updated baseline alongside the
  rule change in the same PR. The new ``rule_version`` is also recorded.

The harness deliberately tests *only* the rule overlay, not the full
text→polarity→label pipeline. Polarity scoring quality is a separate
concern that needs its own end-to-end gold-set with real text. See ADR
``adr-eval-harness-political-sentiment``.

XFails: rows whose ``xfail_reason`` is non-empty are excluded from the
metrics and counted separately. They document known rule limitations
that should not regress (an xfailed case predicting the expected label
is reported as ``xpass`` — investigate before flipping it to a regular
case, since it may indicate the limitation has been fixed).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mapear_nlp.political_sentiment import (
    MODEL_VERSION,
    ClassificationThresholds,
    PoliticalSentimentClassifier,
    SentimentLabel,
)

ROOT = Path(__file__).resolve().parent
GOLD_CSV = ROOT / "gold_set.csv"
BASELINE_JSON = ROOT / "baseline.json"

LABELS: tuple[SentimentLabel, ...] = ("FAVORABLE", "WARNING", "ALERT")


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    polarity: float
    volume_24h: int
    velocity: float
    engagement: int
    recurrence: float
    expected_label: SentimentLabel
    description: str
    xfail_reason: str


def _load_cases(path: Path = GOLD_CSV) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cases.append(
                EvalCase(
                    case_id=row["case_id"],
                    polarity=float(row["polarity"]),
                    volume_24h=int(row["volume_24h"]),
                    velocity=float(row["velocity"]),
                    engagement=int(row["engagement"]),
                    recurrence=float(row["recurrence"]),
                    expected_label=row["expected_label"],
                    description=row["description"],
                    xfail_reason=(row.get("xfail_reason") or "").strip(),
                )
            )
    return cases


def _compute_metrics(confusion: dict[str, Counter]) -> dict[str, Any]:
    """Per-class precision/recall/F1 + macro-F1 + accuracy.

    `confusion[true_label][pred_label]` = count.
    """
    per_class: dict[str, dict[str, float]] = {}
    for label in LABELS:
        tp = confusion[label].get(label, 0)
        fp = sum(confusion[other].get(label, 0) for other in LABELS if other != label)
        fn = sum(confusion[label].get(other, 0) for other in LABELS if other != label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall)
            else 0.0
        )
        per_class[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": tp + fn,
        }

    macro_f1 = sum(per_class[label]["f1"] for label in LABELS) / len(LABELS)
    total = sum(sum(c.values()) for c in confusion.values())
    correct = sum(confusion[label].get(label, 0) for label in LABELS)
    accuracy = correct / total if total else 0.0

    return {
        "f1_macro": round(macro_f1, 4),
        "accuracy": round(accuracy, 4),
        "per_class": per_class,
    }


def evaluate() -> dict[str, Any]:
    cases = _load_cases()
    classifier = PoliticalSentimentClassifier()

    confusion: dict[str, Counter] = {label: Counter() for label in LABELS}
    predictions: dict[str, dict[str, Any]] = {}
    xfailed: list[str] = []
    xpassed: list[str] = []
    misclassified: list[dict[str, Any]] = []

    for case in cases:
        result = classifier.classify(
            polarity=case.polarity,
            volume_24h=case.volume_24h,
            velocity=case.velocity,
            engagement=case.engagement,
            recurrence=case.recurrence,
        )
        pred = result.label
        predictions[case.case_id] = {
            "predicted": pred,
            "expected": case.expected_label,
            "confidence": result.confidence,
            "risk_score": result.risk_score,
            "xfail_reason": case.xfail_reason or None,
        }

        if case.xfail_reason:
            if pred == case.expected_label:
                xpassed.append(case.case_id)
            else:
                xfailed.append(case.case_id)
            continue

        confusion[case.expected_label][pred] += 1
        if pred != case.expected_label:
            misclassified.append(
                {
                    "case_id": case.case_id,
                    "expected": case.expected_label,
                    "predicted": pred,
                    "description": case.description,
                    "inputs": {
                        "polarity": case.polarity,
                        "volume_24h": case.volume_24h,
                        "velocity": case.velocity,
                        "engagement": case.engagement,
                        "recurrence": case.recurrence,
                    },
                }
            )

    return {
        "rule_version": classifier.thresholds.rule_version(),
        "model_version": MODEL_VERSION,
        "n_cases": len(cases),
        "n_xfail": len(xfailed),
        "n_xpass_unexpected": len(xpassed),
        "xfailed_cases": sorted(xfailed),
        "xpassed_cases": sorted(xpassed),
        "confusion": {label: dict(confusion[label]) for label in LABELS},
        "metrics": _compute_metrics(confusion),
        "misclassified": misclassified,
        "predictions": predictions,
    }


def _format_text_report(metrics: dict[str, Any]) -> str:
    m = metrics["metrics"]
    lines = [
        f"=== political-sentiment eval ({metrics['model_version']}) ===",
        f"rule_version: {metrics['rule_version']}",
        f"cases: {metrics['n_cases']}  "
        f"xfail: {metrics['n_xfail']}  "
        f"xpass-unexpected: {metrics['n_xpass_unexpected']}",
        "",
        f"F1 macro:  {m['f1_macro']}",
        f"Accuracy:  {m['accuracy']}",
        "",
        "Per-class:",
    ]
    for label in LABELS:
        pc = m["per_class"][label]
        lines.append(
            f"  {label:10}  precision={pc['precision']:.4f}  "
            f"recall={pc['recall']:.4f}  f1={pc['f1']:.4f}  support={pc['support']}"
        )
    lines.append("")
    lines.append("Confusion (rows=expected, cols=predicted):")
    header = "             " + "  ".join(f"{lab:>10}" for lab in LABELS)
    lines.append(header)
    for label in LABELS:
        row = "  ".join(
            f"{metrics['confusion'][label].get(other, 0):>10}" for other in LABELS
        )
        lines.append(f"  {label:10}  {row}")
    if metrics["misclassified"]:
        lines.append("")
        lines.append("Misclassified:")
        for m_ in metrics["misclassified"]:
            lines.append(
                f"  {m_['case_id']}: expected={m_['expected']} "
                f"got={m_['predicted']} ({m_['description']})"
            )
    if metrics["xpassed_cases"]:
        lines.append("")
        lines.append("XPASS (xfail expected to fail but passed — investigate):")
        for c in metrics["xpassed_cases"]:
            lines.append(f"  {c}")
    return "\n".join(lines)


def _gate_against_baseline(
    metrics: dict[str, Any], baseline: dict[str, Any], max_drop: float
) -> tuple[bool, list[str]]:
    """Return (ok, errors)."""
    errors: list[str] = []
    base_f1 = baseline["metrics"]["f1_macro"]
    cur_f1 = metrics["metrics"]["f1_macro"]
    drop = base_f1 - cur_f1
    if drop > max_drop:
        errors.append(
            f"F1 macro dropped {drop:+.4f} (baseline {base_f1} → current {cur_f1}); "
            f"gate threshold is {max_drop}."
        )

    # Per-case regression: any case that was correct in baseline but is now
    # wrong is a regression even if F1 macro stays within the tolerance.
    base_preds = baseline.get("predictions", {})
    regressions: list[str] = []
    for case_id, cur in metrics["predictions"].items():
        if cur.get("xfail_reason"):
            continue
        prev = base_preds.get(case_id)
        if not prev:
            continue
        was_correct = prev["predicted"] == prev["expected"]
        is_correct = cur["predicted"] == cur["expected"]
        if was_correct and not is_correct:
            regressions.append(
                f"{case_id}: previously {prev['predicted']} (correct), "
                f"now {cur['predicted']} (expected {cur['expected']})"
            )
    if regressions:
        errors.append(
            f"{len(regressions)} case(s) regressed:\n  - " + "\n  - ".join(regressions)
        )
    return (not errors, errors)


def _log_to_mlflow(metrics: dict[str, Any]) -> None:
    """Optional MLflow logging. No-op + friendly hint if mapear-mlops absent."""
    try:
        from mapear_mlops import log_eval_run  # noqa: PLC0415
    except ImportError:
        print(
            "warning: --mlflow requested but mapear-mlops not installed; "
            "run `poetry -C mapear-mlops install` and add it to "
            "mapear-nlp dev deps. Skipping MLflow log.",
            file=sys.stderr,
        )
        return

    threshold_params = dataclasses.asdict(ClassificationThresholds())
    log_eval_run(
        metrics,
        extra_params=threshold_params,
        extra_artifacts=[GOLD_CSV],
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Overwrite baseline.json with current metrics. "
        "Use after a deliberate rule change.",
    )
    parser.add_argument(
        "--max-f1-drop",
        type=float,
        default=0.05,
        help="Fail if baseline F1 macro - current F1 macro > this. Default 0.05.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--mlflow",
        action="store_true",
        help="Log this run to MLflow via mapear-mlops. "
        "Tracking URI defaults to ./mlruns at the monorepo root.",
    )
    args = parser.parse_args(argv)

    metrics = evaluate()

    if args.mlflow:
        _log_to_mlflow(metrics)

    if args.update_baseline:
        BASELINE_JSON.write_text(json.dumps(metrics, indent=2) + "\n")
        print(_format_text_report(metrics))
        print(f"\nBaseline updated → {BASELINE_JSON.relative_to(ROOT.parent.parent)}")
        return 0

    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print(_format_text_report(metrics))

    if not BASELINE_JSON.exists():
        print(
            f"\nNo baseline at {BASELINE_JSON}. "
            "Run `make eval-update-baseline` to create it.",
            file=sys.stderr,
        )
        return 2

    baseline = json.loads(BASELINE_JSON.read_text())
    ok, errors = _gate_against_baseline(metrics, baseline, args.max_f1_drop)
    if ok:
        print("\nOK: no regression vs baseline.")
        return 0
    print("\nFAIL: eval gate violations:", file=sys.stderr)
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
