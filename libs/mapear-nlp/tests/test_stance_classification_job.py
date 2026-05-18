"""Integration tests for the stance-classification job — Eixo 2 v2b.

The job is exercised end-to-end (JSONL in → JSONL out) with a stub LLM
client that echoes a configurable stance label. No real LLM call is made.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class _StubLLMClient:
    """Returns a pre-configured stance JSON string for every call."""

    provider = "stub"

    def __init__(self, stance: str = "contra", confidence: str = "high") -> None:
        self.model = "stub-model"
        self.calls: list[str] = []
        self._response = f'{{"stance": "{stance}", "confidence": "{confidence}"}}'

    def complete(self, prompt, *, max_tokens, temperature, timeout_seconds) -> str:
        self.calls.append(prompt)
        return self._response


class _NoopCache:
    def get(self, key: str) -> dict | None:
        return None

    def set(self, key: str, payload: dict) -> None:
        pass


def _write_gold(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _gold_row(
    content_hash: str,
    narrative: str | None = "O prefeito corta gastos na saúde.",
    region: str = "rn",
    person_id: str = "mayor_x",
    person_name: str = "X",
    role: str = "prefeito",
) -> dict[str, Any]:
    return {
        "content_hash": content_hash,
        "narrative_summary": narrative,
        "region": region,
        "person_id": person_id,
        "person_name": person_name,
        "role": role,
        "rule_version": "v1",
        "source_type": "rss",
        "tenant_id": None,
    }


def test_job_emits_stance_row_per_narrative(tmp_path: Path) -> None:
    from mapear_nlp.run_stance_classification import run

    gold_path = tmp_path / "gold.jsonl"
    out_path = tmp_path / "stances.jsonl"
    _write_gold(gold_path, [_gold_row("h1"), _gold_row("h2"), _gold_row("h3")])

    n = run(
        gold_path,
        out_path,
        region_filter=None,
        pipeline_version="test",
        llm_client=_StubLLMClient("contra", "high"),
        cache=_NoopCache(),
    )

    assert n == 3
    rows = _read_jsonl(out_path)
    assert len(rows) == 3
    for r in rows:
        assert r["stance_label"] == "contra"
        assert r["confidence"] == "high"
        assert r["cache_hit"] is False
        assert r["error"] is None
        assert r["pipeline_version"] == "test"


def test_job_skips_rows_without_narrative_summary(tmp_path: Path) -> None:
    from mapear_nlp.run_stance_classification import run

    gold_path = tmp_path / "gold.jsonl"
    out_path = tmp_path / "stances.jsonl"
    _write_gold(
        gold_path,
        [
            _gold_row("h1"),
            _gold_row("h2", narrative=None),  # no summary → skipped
            _gold_row("h3", narrative=""),  # empty summary → skipped
            _gold_row("h4"),
        ],
    )

    n = run(
        gold_path,
        out_path,
        region_filter=None,
        pipeline_version="test",
        llm_client=_StubLLMClient(),
        cache=_NoopCache(),
    )

    assert n == 2
    hashes = {r["content_hash"] for r in _read_jsonl(out_path)}
    assert hashes == {"h1", "h4"}


def test_job_region_filter(tmp_path: Path) -> None:
    from mapear_nlp.run_stance_classification import run

    gold_path = tmp_path / "gold.jsonl"
    out_path = tmp_path / "stances.jsonl"
    _write_gold(
        gold_path,
        [
            _gold_row("h1", region="rn"),
            _gold_row("h2", region="pe"),
            _gold_row("h3", region="rn"),
        ],
    )

    n = run(
        gold_path,
        out_path,
        region_filter="rn",
        pipeline_version="test",
        llm_client=_StubLLMClient(),
        cache=_NoopCache(),
    )

    assert n == 2
    regions = {r["region"] for r in _read_jsonl(out_path)}
    assert regions == {"rn"}


def test_job_empty_input_returns_zero(tmp_path: Path) -> None:
    from mapear_nlp.run_stance_classification import run

    gold_path = tmp_path / "gold.jsonl"
    out_path = tmp_path / "stances.jsonl"
    gold_path.write_text("")

    n = run(
        gold_path,
        out_path,
        region_filter=None,
        pipeline_version="test",
        llm_client=_StubLLMClient(),
        cache=_NoopCache(),
    )

    assert n == 0


def test_job_carries_person_fields(tmp_path: Path) -> None:
    from mapear_nlp.run_stance_classification import run

    gold_path = tmp_path / "gold.jsonl"
    out_path = tmp_path / "stances.jsonl"
    _write_gold(
        gold_path,
        [_gold_row("h1", person_id="mayor_x", person_name="X", role="prefeito")],
    )

    run(
        gold_path,
        out_path,
        region_filter=None,
        pipeline_version="test",
        llm_client=_StubLLMClient("favor"),
        cache=_NoopCache(),
    )

    row = _read_jsonl(out_path)[0]
    assert row["person_id"] == "mayor_x"
    assert row["person_name"] == "X"
    assert row["person_role"] == "prefeito"
    assert row["stance_label"] == "favor"


def test_job_stance_disabled_returns_zero(tmp_path: Path) -> None:
    from mapear_nlp.run_stance_classification import run

    gold_path = tmp_path / "gold.jsonl"
    out_path = tmp_path / "stances.jsonl"
    _write_gold(gold_path, [_gold_row("h1")])

    n = run(
        gold_path,
        out_path,
        region_filter=None,
        pipeline_version="test",
        llm_client=_StubLLMClient(),
        cache=_NoopCache(),
        stance_enabled=False,
    )

    assert n == 0
    assert not out_path.exists()
