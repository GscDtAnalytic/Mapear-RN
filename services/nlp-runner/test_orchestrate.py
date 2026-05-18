"""Unit tests for the nlp-runner orchestrator.

google-cloud-bigquery is only installed in the runner image (gcp poetry
group), so we ``importorskip`` it here — dev environments without the
SDK still pass.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("google.cloud.bigquery")

sys.path.insert(0, os.path.dirname(__file__))

import orchestrate  # noqa: E402


def test_required_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    with pytest.raises(SystemExit, match="DOES_NOT_EXIST"):
        orchestrate._required_env("DOES_NOT_EXIST")


def test_required_env_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "value")
    assert orchestrate._required_env("X") == "value"


def test_json_safe_datetime() -> None:
    ts = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    assert orchestrate._json_safe(ts) == ts.isoformat()


def test_json_safe_nested_list_with_datetime() -> None:
    ts = datetime(2026, 5, 11, 12, 0, 0, tzinfo=UTC)
    result = orchestrate._json_safe([1, "two", ts, [ts, "four"]])
    assert result == [1, "two", ts.isoformat(), [ts.isoformat(), "four"]]


def test_json_safe_passes_through_scalars() -> None:
    assert orchestrate._json_safe("hello") == "hello"
    assert orchestrate._json_safe(42) == 42
    assert orchestrate._json_safe(None) is None
    assert orchestrate._json_safe(True) is True


def test_load_schema_reads_real_contract(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "infra/modules/bigquery/schemas"
    monkey_dir = tmp_path / "schemas"
    monkey_dir.mkdir()

    source = schema_path / "silver_article_stances.json"
    target = monkey_dir / "silver_article_stances.json"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    with mock.patch.object(orchestrate, "SCHEMAS_DIR", monkey_dir):
        schema = orchestrate._load_schema("silver_article_stances")

    assert len(schema) == 18
    names = [f.name for f in schema]
    assert "content_hash" in names
    assert "stance_label" in names
    assert "stance_prompt_version" in names


def test_extract_gold_writes_jsonl(tmp_path: Path) -> None:
    fake_rows = [
        {
            "content_hash": "abc",
            "narrative_summary": "texto",
            "narrative_prompt_version": "v1",
            "rule_version": "r1",
            "published_at": datetime(2026, 5, 11, tzinfo=UTC),
            "person_id": "p1",
            "person_name": "Fulano",
            "role": "prefeito",
            "source_type": "rss",
            "region": "rn",
            "tenant_id": None,
        }
    ]

    class _FakeResult:
        def __iter__(self) -> object:
            return iter(fake_rows)

    class _FakeQueryJob:
        def result(self) -> _FakeResult:
            return _FakeResult()

    fake_client = mock.Mock()
    fake_client.query.return_value = _FakeQueryJob()

    out = tmp_path / "gold.jsonl"
    count = orchestrate._extract_gold_to_jsonl(
        fake_client, "proj", "gold_ds", "rn", None, out
    )

    assert count == 1
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert row["content_hash"] == "abc"
    assert row["published_at"] == "2026-05-11T00:00:00+00:00"


def test_main_rejects_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NLP_JOB", "make-coffee")
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCP_BQ_DATASET_GOLD", "g")
    monkeypatch.setenv("GCP_BQ_DATASET_SILVER", "s")

    with mock.patch.object(orchestrate.bigquery, "Client") as m:
        m.return_value = mock.Mock()
        with pytest.raises(SystemExit, match="unknown NLP_JOB"):
            orchestrate.main()


def test_main_requires_nlp_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NLP_JOB", raising=False)
    with pytest.raises(SystemExit, match="NLP_JOB"):
        orchestrate.main()
