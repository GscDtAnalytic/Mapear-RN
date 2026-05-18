"""Stance-classifier regression gate — Eixo 2 v2b.

Reads ``stance_gold_set.csv``, runs ``StanceClassifier`` against each row,
and reports accuracy + per-case diff table. Exits non-zero if accuracy
drops below ``--min-accuracy`` (default 0.80).

Usage::

    poetry run python -m mapear_nlp.eval.stance_run \\
        --gold eval/stance_gold_set.csv \\
        --min-accuracy 0.80
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent


def _load_gold(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _run_eval(
    rows: list[dict[str, str]],
    llm_client: Any,
    cache: Any,
) -> list[dict[str, Any]]:
    from mapear_nlp.stance_classifier import StanceClassifier

    classifier = StanceClassifier(
        llm_client,
        cache,
        max_tokens=60,
        temperature=0.0,
    )

    results = []
    for row in rows:
        result = classifier.classify(
            content_hash=row["content_hash"],
            narrative_summary=row["narrative_summary"],
            person_name=row["person_name"],
            person_role=row["person_role"],
            rule_version="eval",
        )
        expected = row["expected_stance"]
        passed = result.stance_label == expected
        results.append(
            {
                "case_id": row["case_id"],
                "expected": expected,
                "got": result.stance_label,
                "confidence": result.confidence,
                "error": result.error,
                "passed": passed,
                "notes": row.get("notes", ""),
            }
        )
    return results


def _report(results: list[dict[str, Any]], min_accuracy: float) -> bool:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    accuracy = passed / total if total else 0.0

    print(f"\nStance eval: {passed}/{total} passed ({accuracy:.0%})\n")
    print(f"{'case_id':<8} {'expected':<10} {'got':<10} {'conf':<8} {'ok':<4} notes")
    print("-" * 72)
    for r in results:
        ok = "OK" if r["passed"] else "FAIL"
        err = f" [err: {r['error']}]" if r["error"] else ""
        print(
            f"{r['case_id']:<8} {r['expected']:<10} {(r['got'] or 'None'):<10}"
            f" {(r['confidence'] or '?'):<8} {ok:<4} {r['notes']}{err}"
        )

    meets_threshold = accuracy >= min_accuracy
    if not meets_threshold:
        print(
            f"\nFAIL: accuracy {accuracy:.0%} < threshold {min_accuracy:.0%}",
            file=sys.stderr,
        )
    else:
        print(f"\nPASS: accuracy {accuracy:.0%} >= threshold {min_accuracy:.0%}")
    return meets_threshold


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Stance classifier regression gate")
    p.add_argument(
        "--gold",
        type=Path,
        default=_ROOT / "stance_gold_set.csv",
        help="Gold-set CSV path",
    )
    p.add_argument(
        "--min-accuracy",
        type=float,
        default=0.80,
        help="Minimum fraction correct to pass (default: 0.80)",
    )
    args = p.parse_args(argv)

    rows = _load_gold(args.gold)

    from mapear_infra.config import get_settings

    from mapear_nlp.llm.client import get_llm_client

    settings = get_settings()
    llm_client = get_llm_client(settings.llm)

    results = _run_eval(rows, llm_client, cache=None)
    ok = _report(results, args.min_accuracy)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
