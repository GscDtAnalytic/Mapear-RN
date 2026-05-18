"""Unit tests for the embed-social orchestrator.

google-cloud-bigquery is only installed in the runner image (gcp poetry
group), so we importorskip it here — dev environments without the SDK
still pass.
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
    ts = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    assert orchestrate._json_safe(ts) == ts.isoformat()


def test_json_safe_scalars() -> None:
    assert orchestrate._json_safe("hello") == "hello"
    assert orchestrate._json_safe(42) == 42
    assert orchestrate._json_safe(None) is None


def test_extract_social_to_jsonl(tmp_path: Path) -> None:
    fake_rows = [
        {
            "content_hash": "h1",
            "text": "Olá mundo",
            "published_at": datetime(2026, 5, 14, 9, 0, tzinfo=UTC),
            "region": "rn",
            "tenant_id": None,
        },
        {
            "content_hash": "h2",
            "text": "Segundo post",
            "published_at": datetime(2026, 5, 13, 8, 0, tzinfo=UTC),
            "region": "rn",
            "tenant_id": None,
        },
    ]

    class _FakeResult:
        def __iter__(self) -> object:
            return iter(fake_rows)

    class _FakeJob:
        def result(self) -> _FakeResult:
            return _FakeResult()

    client = mock.Mock()
    client.query.return_value = _FakeJob()

    out = tmp_path / "posts.jsonl"
    count = orchestrate._extract_social_to_jsonl(client, "proj", "silver", "rn", 2, out)

    assert count == 2
    lines = out.read_text().splitlines()
    row0 = json.loads(lines[0])
    assert row0["content_hash"] == "h1"
    assert row0["published_at"] == "2026-05-14T09:00:00+00:00"


def test_embed_social_posts_with_cache(tmp_path: Path) -> None:
    posts_path = tmp_path / "posts.jsonl"
    out_path = tmp_path / "embeddings.jsonl"
    cmds_seen: list[list[str]] = []

    def _fake_run(cmd: list[str], *, check: bool) -> None:
        cmds_seen.append(cmd)

    with mock.patch.object(orchestrate.subprocess, "run", side_effect=_fake_run):
        orchestrate._embed_social_posts(
            posts_path, out_path, "rn", "my-bucket", "social_post_embeddings/", "proj"
        )

    assert len(cmds_seen) == 1
    cmd = cmds_seen[0]
    assert "--region" in cmd
    assert "rn" in cmd
    assert "--cache-bucket" in cmd
    assert "my-bucket" in cmd
    assert "--no-cache" not in cmd


def test_embed_social_posts_no_cache(tmp_path: Path) -> None:
    posts_path = tmp_path / "posts.jsonl"
    out_path = tmp_path / "embeddings.jsonl"
    cmds_seen: list[list[str]] = []

    def _fake_run(cmd: list[str], *, check: bool) -> None:
        cmds_seen.append(cmd)

    with mock.patch.object(orchestrate.subprocess, "run", side_effect=_fake_run):
        orchestrate._embed_social_posts(
            posts_path, out_path, "rn", "", "social_post_embeddings/", "proj"
        )

    cmd = cmds_seen[0]
    assert "--no-cache" in cmd
    assert "--cache-bucket" not in cmd


def test_main_requires_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    with pytest.raises(SystemExit, match="GCP_PROJECT_ID"):
        orchestrate.main()


def test_main_requires_silver_ds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.delenv("GCP_BQ_DATASET_SILVER", raising=False)
    with pytest.raises(SystemExit, match="GCP_BQ_DATASET_SILVER"):
        orchestrate.main()


def test_main_skips_when_no_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("GCP_BQ_DATASET_SILVER", "silver")

    class _FakeResult:
        def __iter__(self) -> object:
            return iter([])

    class _FakeJob:
        def result(self) -> _FakeResult:
            return _FakeResult()

    fake_client = mock.Mock()
    fake_client.query.return_value = _FakeJob()

    with (
        mock.patch.object(orchestrate.bigquery, "Client", return_value=fake_client),
        mock.patch.object(orchestrate.subprocess, "run") as m_run,
    ):
        result = orchestrate.main()

    assert result == 0
    m_run.assert_not_called()


def test_main_skips_bq_load_when_embeddings_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("GCP_BQ_DATASET_SILVER", "silver")

    fake_rows = [
        {
            "content_hash": "h1",
            "text": "Post.",
            "published_at": datetime(2026, 5, 14, tzinfo=UTC),
            "region": "rn",
            "tenant_id": None,
        }
    ]

    class _FakeResult:
        def __iter__(self) -> object:
            return iter(fake_rows)

    class _FakeJob:
        def result(self) -> _FakeResult:
            return _FakeResult()

    fake_client = mock.Mock()
    fake_client.query.return_value = _FakeJob()

    loaded_tables: list[str] = []

    def _fake_load(client: object, path: Path, table_id: str, schema_table: str) -> int:
        loaded_tables.append(table_id)
        return 1

    # run_social_embedding writes nothing (disabled / empty batch).
    with (
        mock.patch.object(orchestrate.bigquery, "Client", return_value=fake_client),
        mock.patch.object(orchestrate.subprocess, "run"),
        mock.patch.object(orchestrate, "_load_jsonl_to_bq", side_effect=_fake_load),
    ):
        result = orchestrate.main()

    assert result == 0
    assert not loaded_tables  # no BQ load when embeddings file is absent
