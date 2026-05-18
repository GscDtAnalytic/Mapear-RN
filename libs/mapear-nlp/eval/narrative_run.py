"""Narrative explainer eval — Eixo 2 v1.

Opt-in evaluator: real LLM calls cost money, so this is not part of
the CI eval-gate. Run locally:

    make narrative-eval        # against gold_set with default prompt
    MAPEAR_LLM_API_KEY=...  poetry run python -m eval.narrative_run

For each gold case, render the prompt, call the configured LLM, then
check the response against ``must_contain_any``, ``must_not_contain_any``,
and length bounds. Exits non-zero if any case fails.

This is deliberately lo-fi. Stronger evals (BLEU vs gold, LLM-as-judge,
factuality checks) are deferred to Eixo 2 v2 once the prompt has been
iterated on real production rows.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from mapear_infra.config import get_settings

from mapear_nlp.llm.client import get_llm_client
from mapear_nlp.narrative_explainer import NarrativeExplainer

_GOLD_SET = Path(__file__).resolve().parent / "narrative_gold_set.csv"


def _load_gold_set(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _check_case(case: dict, summary: str) -> list[str]:
    failures = []
    must_any = [s.strip() for s in case["must_contain_any"].split(";") if s.strip()]
    if must_any and not any(s in summary for s in must_any):
        failures.append(f"missing all of: {must_any}")
    must_not = [s.strip() for s in case["must_not_contain_any"].split(";") if s.strip()]
    hits = [s for s in must_not if s in summary]
    if hits:
        failures.append(f"contains forbidden: {hits}")
    min_len, max_len = int(case["min_length"]), int(case["max_length"])
    if len(summary) < min_len:
        failures.append(f"too short ({len(summary)} < {min_len})")
    if len(summary) > max_len:
        failures.append(f"too long ({len(summary)} > {max_len})")
    return failures


def run(gold_path: Path = _GOLD_SET) -> int:
    settings = get_settings()
    if not settings.llm.api_key and not settings.llm.api_key_secret:
        sys.stderr.write("ERROR: set MAPEAR_LLM_API_KEY or MAPEAR_LLM_API_KEY_SECRET\n")
        return 2
    client = get_llm_client(settings.llm)
    explainer = NarrativeExplainer(
        llm_client=client,
        cache=None,  # eval should always exercise the LLM, not the cache
        max_tokens=settings.llm.max_tokens,
        temperature=settings.llm.temperature,
        timeout_seconds=settings.llm.timeout_seconds,
    )
    cases = _load_gold_set(gold_path)
    total = len(cases)
    failed = 0
    for case in cases:
        result = explainer.explain(
            content_hash=case["content_hash"],
            title=case["title"],
            content=case["content"],
            person_name=case["person_name"],
            person_role=case["person_role"],
            polarity=float(case["polarity"]),
            velocity=float(case["velocity"]),
            volume=int(case["volume"]),
            decision_factors=[],
            rule_version="eval",
        )
        if not result.summary:
            sys.stdout.write(f"FAIL {case['case_id']}: no summary ({result.error})\n")
            failed += 1
            continue
        problems = _check_case(case, result.summary)
        if problems:
            sys.stdout.write(f"FAIL {case['case_id']}: {problems}\n")
            sys.stdout.write(f"  summary: {result.summary}\n")
            failed += 1
        else:
            sys.stdout.write(f"PASS {case['case_id']}\n")
    sys.stdout.write(f"\nNarrative eval: {total - failed}/{total} passed\n")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Narrative explainer eval")
    parser.add_argument(
        "--gold",
        type=Path,
        default=_GOLD_SET,
        help="Path to narrative_gold_set.csv (default: bundled)",
    )
    args = parser.parse_args()
    return run(args.gold)


if __name__ == "__main__":
    sys.exit(main())
