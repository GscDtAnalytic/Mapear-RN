"""Shadow A/B comparator for the political-sentiment overlay.

Stage 1E v1 — Python-only. Runs the live ``PoliticalSentimentClassifier``
twice on the same input batch with two different
``ClassificationThresholds`` (primary and candidate), then summarizes the
divergence: label distribution shift, transition matrix, confidence /
risk shifts, and a per-case diff.

The v2 evolution (write-once-per-event into ``mapear_events_shadow``
plus a dbt mart) is documented in
``docs/decisions/adr-shadow-scoring-stage-1e.md``. The v1 here is the
operator-controlled flow: take a CSV of cases (the gold-set or a BQ
export), point at a candidate YAML, get a comparison report + optional
MLflow run.

Severity ordering for movement counts:
    FAVORABLE < WARNING < ALERT

So "escalated" = primary→candidate moves up this ladder (FAVORABLE→
WARNING, FAVORABLE→ALERT, WARNING→ALERT); "demoted" moves down.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mapear_nlp.political_sentiment import (
    ClassificationResult,
    ClassificationThresholds,
    PoliticalSentimentClassifier,
    SentimentLabel,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "gold_set.csv"

LABELS: tuple[SentimentLabel, ...] = ("FAVORABLE", "WARNING", "ALERT")
_SEVERITY: dict[str, int] = {"FAVORABLE": 0, "WARNING": 1, "ALERT": 2}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputCase:
    """One row of inputs to the classifier.

    The CSV reader fills ``case_id`` from the column when present and
    falls back to the row number otherwise. Optional metadata
    (``description``, ``expected_label``) is preserved through to the
    report so operators can grep movement against original intent.
    """

    case_id: str
    polarity: float
    volume_24h: int
    velocity: float
    engagement: int
    recurrence: float
    description: str = ""
    expected_label: str = ""


def load_cases(path: Path) -> list[InputCase]:
    cases: list[InputCase] = []
    with path.open(newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            cases.append(
                InputCase(
                    case_id=row.get("case_id") or f"row-{i + 1}",
                    polarity=float(row["polarity"]),
                    volume_24h=int(row["volume_24h"]),
                    velocity=float(row["velocity"]),
                    engagement=int(row["engagement"]),
                    recurrence=float(row.get("recurrence") or 0),
                    description=(row.get("description") or "").strip(),
                    expected_label=(row.get("expected_label") or "").strip(),
                )
            )
    return cases


def load_thresholds(path: Path | None) -> ClassificationThresholds:
    """Load thresholds from YAML, falling back to defaults for missing fields.

    The YAML need only list the keys the operator wants to override:

        polarity_negative: -0.40   # tightened from -0.35
        velocity_spike: 0.8
    """
    if path is None:
        return ClassificationThresholds()
    data = yaml.safe_load(path.read_text()) or {}
    defaults = dataclasses.asdict(ClassificationThresholds())
    unknown = set(data) - set(defaults)
    if unknown:
        raise ValueError(
            f"Unknown threshold keys in {path}: {sorted(unknown)}. "
            f"Allowed: {sorted(defaults)}"
        )
    defaults.update(data)
    return ClassificationThresholds(**defaults)


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowRecord:
    """One case classified under both regimes."""

    case: InputCase
    primary: ClassificationResult
    candidate: ClassificationResult


def run_shadow(
    cases: list[InputCase],
    primary: ClassificationThresholds,
    candidate: ClassificationThresholds,
) -> list[ShadowRecord]:
    p_clf = PoliticalSentimentClassifier(primary)
    c_clf = PoliticalSentimentClassifier(candidate)
    records: list[ShadowRecord] = []
    for case in cases:
        kwargs = {
            "polarity": case.polarity,
            "volume_24h": case.volume_24h,
            "velocity": case.velocity,
            "engagement": case.engagement,
            "recurrence": case.recurrence,
        }
        records.append(
            ShadowRecord(
                case=case,
                primary=p_clf.classify(**kwargs),
                candidate=c_clf.classify(**kwargs),
            )
        )
    return records


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass
class ComparisonReport:
    n_cases: int = 0
    primary_rule_version: str = ""
    candidate_rule_version: str = ""
    primary_distribution: dict[str, int] = field(default_factory=dict)
    candidate_distribution: dict[str, int] = field(default_factory=dict)
    transition_matrix: dict[str, dict[str, int]] = field(default_factory=dict)
    agreed: int = 0
    escalated: int = 0
    demoted: int = 0
    mean_confidence_shift: float = 0.0
    mean_risk_shift: float = 0.0
    movements: list[dict[str, Any]] = field(default_factory=list)


def compare(records: list[ShadowRecord]) -> ComparisonReport:
    report = ComparisonReport(n_cases=len(records))
    if not records:
        return report

    report.primary_rule_version = records[0].primary.rule_version
    report.candidate_rule_version = records[0].candidate.rule_version

    primary_counter: Counter[str] = Counter()
    candidate_counter: Counter[str] = Counter()
    transitions: dict[str, Counter[str]] = {label: Counter() for label in LABELS}
    confidence_deltas: list[float] = []
    risk_deltas: list[float] = []

    for r in records:
        p_label = r.primary.label
        c_label = r.candidate.label
        primary_counter[p_label] += 1
        candidate_counter[c_label] += 1
        transitions[p_label][c_label] += 1
        confidence_deltas.append(r.candidate.confidence - r.primary.confidence)
        risk_deltas.append(r.candidate.risk_score - r.primary.risk_score)

        if p_label == c_label:
            report.agreed += 1
        else:
            if _SEVERITY[c_label] > _SEVERITY[p_label]:
                report.escalated += 1
            else:
                report.demoted += 1
            report.movements.append(
                {
                    "case_id": r.case.case_id,
                    "primary_label": p_label,
                    "candidate_label": c_label,
                    "description": r.case.description,
                    "expected_label": r.case.expected_label,
                    "inputs": {
                        "polarity": r.case.polarity,
                        "volume_24h": r.case.volume_24h,
                        "velocity": r.case.velocity,
                        "engagement": r.case.engagement,
                        "recurrence": r.case.recurrence,
                    },
                    "confidence_shift": round(
                        r.candidate.confidence - r.primary.confidence, 4
                    ),
                    "risk_shift": round(
                        r.candidate.risk_score - r.primary.risk_score, 4
                    ),
                }
            )

    report.primary_distribution = {
        label: primary_counter.get(label, 0) for label in LABELS
    }
    report.candidate_distribution = {
        label: candidate_counter.get(label, 0) for label in LABELS
    }
    report.transition_matrix = {
        primary_label: {
            candidate_label: transitions[primary_label].get(candidate_label, 0)
            for candidate_label in LABELS
        }
        for primary_label in LABELS
    }
    report.mean_confidence_shift = round(
        sum(confidence_deltas) / len(confidence_deltas), 4
    )
    report.mean_risk_shift = round(sum(risk_deltas) / len(risk_deltas), 4)
    return report


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_text(
    report: ComparisonReport,
    *,
    primary: ClassificationThresholds,
    candidate: ClassificationThresholds,
) -> str:
    diff = _threshold_diff(primary, candidate)
    lines = [
        "=== political-sentiment shadow ===",
        f"primary rule_version:   {report.primary_rule_version}",
        f"candidate rule_version: {report.candidate_rule_version}",
        f"cases:    {report.n_cases}",
        f"agreed:   {report.agreed}",
        f"escalated:{report.escalated}",
        f"demoted:  {report.demoted}",
        f"mean confidence shift: {report.mean_confidence_shift:+.4f}",
        f"mean risk shift:       {report.mean_risk_shift:+.4f}",
        "",
        "Threshold diffs (candidate - primary):",
    ]
    if not diff:
        lines.append("  (none — candidate is identical to primary)")
    else:
        for key, (a, b) in diff.items():
            lines.append(f"  {key}: {a} → {b}")
    lines.extend(["", "Label distribution:"])
    lines.append(f"  {'label':10}  {'primary':>8}  {'candidate':>10}  {'delta':>7}")
    for label in LABELS:
        p = report.primary_distribution.get(label, 0)
        c = report.candidate_distribution.get(label, 0)
        lines.append(f"  {label:10}  {p:>8}  {c:>10}  {c - p:>+7}")
    lines.extend(
        [
            "",
            "Transition matrix (rows=primary, cols=candidate):",
            "             " + "  ".join(f"{label:>10}" for label in LABELS),
        ]
    )
    for primary_label in LABELS:
        cells = "  ".join(
            f"{report.transition_matrix[primary_label].get(candidate_label, 0):>10}"
            for candidate_label in LABELS
        )
        lines.append(f"  {primary_label:10}  {cells}")
    if report.movements:
        lines.append("")
        lines.append("Movements (top 20):")
        for m in report.movements[:20]:
            lines.append(
                f"  {m['case_id']:8} {m['primary_label']:>9} → "
                f"{m['candidate_label']:<9} "
                f"Δconf={m['confidence_shift']:+.3f}  ({m['description'][:60]})"
            )
        if len(report.movements) > 20:
            lines.append(f"  ... +{len(report.movements) - 20} more")
    return "\n".join(lines)


def _threshold_diff(
    primary: ClassificationThresholds, candidate: ClassificationThresholds
) -> dict[str, tuple[Any, Any]]:
    a = dataclasses.asdict(primary)
    b = dataclasses.asdict(candidate)
    return {k: (a[k], b[k]) for k in a if a[k] != b[k]}


def _report_to_json(
    report: ComparisonReport,
    *,
    primary: ClassificationThresholds,
    candidate: ClassificationThresholds,
) -> dict[str, Any]:
    return {
        **dataclasses.asdict(report),
        "threshold_diff": {
            k: {"primary": a, "candidate": b}
            for k, (a, b) in _threshold_diff(primary, candidate).items()
        },
    }


# ---------------------------------------------------------------------------
# MLflow logging
# ---------------------------------------------------------------------------


def _log_to_mlflow(
    report: ComparisonReport,
    *,
    primary: ClassificationThresholds,
    candidate: ClassificationThresholds,
    full_json: dict[str, Any],
) -> None:
    try:
        from mapear_mlops.tracking import log_eval_run  # noqa: PLC0415
    except ImportError:
        print(
            "warning: --mlflow requested but mapear-mlops not installed; "
            "run `poetry -C mapear-mlops install`. Skipping MLflow log.",
            file=sys.stderr,
        )
        return

    # Reuse the eval-run logger by shaping the metrics dict in the
    # convention it expects. The logger picks up rule_version + model_version
    # tags from the dict; we add shadow-specific fields below.
    rule_version = f"{report.primary_rule_version}__vs__{report.candidate_rule_version}"
    metrics_for_log: dict[str, Any] = {
        "rule_version": rule_version,
        "model_version": "political-sentiment-shadow",
        "n_cases": report.n_cases,
        "metrics": {
            "agreed": report.agreed,
            "escalated": report.escalated,
            "demoted": report.demoted,
            "mean_confidence_shift": report.mean_confidence_shift,
            "mean_risk_shift": report.mean_risk_shift,
            "per_class": {
                label: {
                    "primary_count": report.primary_distribution.get(label, 0),
                    "candidate_count": report.candidate_distribution.get(label, 0),
                }
                for label in LABELS
            },
        },
    }
    # Log primary + candidate params with prefixes so they survive
    # the MLflow constraint that param keys must be unique per run.
    extra_params: dict[str, Any] = {}
    for k, v in dataclasses.asdict(primary).items():
        extra_params[f"primary__{k}"] = v
    for k, v in dataclasses.asdict(candidate).items():
        extra_params[f"candidate__{k}"] = v

    log_eval_run(
        metrics_for_log,
        experiment="mapear-political-sentiment-shadow",
        extra_params=extra_params,
        run_name=f"shadow_{report.candidate_rule_version[:8]}",
        extra_artifact_dicts={"shadow_comparison.json": full_json},
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--candidate",
        type=Path,
        required=True,
        help="YAML file with the candidate thresholds (overrides defaults).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"CSV of input cases. Defaults to {DEFAULT_INPUT.name}.",
    )
    parser.add_argument(
        "--primary",
        type=Path,
        default=None,
        help="YAML with primary thresholds. Defaults to the live "
        "ClassificationThresholds() defaults.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--mlflow",
        action="store_true",
        help="Log this comparison to MLflow via mapear-mlops.",
    )
    args = parser.parse_args(argv)

    primary = load_thresholds(args.primary)
    candidate = load_thresholds(args.candidate)
    cases = load_cases(args.input)
    records = run_shadow(cases, primary, candidate)
    report = compare(records)

    full_json = _report_to_json(report, primary=primary, candidate=candidate)
    if args.json:
        print(json.dumps(full_json, indent=2))
    else:
        print(_format_text(report, primary=primary, candidate=candidate))

    if args.mlflow:
        _log_to_mlflow(
            report, primary=primary, candidate=candidate, full_json=full_json
        )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
