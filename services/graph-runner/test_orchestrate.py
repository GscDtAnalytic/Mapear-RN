"""Unit tests for the graph-runner orchestrator.

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


def test_bool_env_parses_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    for truthy in ("1", "true", "yes", "on", "TRUE", "YES"):
        monkeypatch.setenv("X", truthy)
        assert orchestrate._bool_env("X", default=False) is True


def test_bool_env_parses_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("X", falsy)
        assert orchestrate._bool_env("X", default=True) is False


def test_bool_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X", raising=False)
    assert orchestrate._bool_env("X", default=True) is True
    assert orchestrate._bool_env("X", default=False) is False


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
    """The runner expects schemas/<table>.json files baked into the image."""
    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "infra/modules/bigquery/schemas"
    monkey_dir = tmp_path / "schemas"
    monkey_dir.mkdir()

    # Stage a known schema into a tmp dir to keep the test hermetic.
    source = schema_path / "silver_author_personas.json"
    target = monkey_dir / "silver_author_personas.json"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    with mock.patch.object(orchestrate, "SCHEMAS_DIR", monkey_dir):
        schema = orchestrate._load_schema("silver_author_personas")

    assert len(schema) == 17
    names = [f.name for f in schema]
    assert "persona_id" in names
    assert "platform" in names
    assert "evidence_json" in names


def test_extract_query_writes_jsonl(tmp_path: Path) -> None:
    """_extract_query_to_jsonl writes one JSON line per row and counts them."""
    fake_rows = [
        {"a": 1, "b": "x", "ts": datetime(2026, 5, 11, tzinfo=UTC)},
        {"a": 2, "b": "y", "ts": datetime(2026, 5, 12, tzinfo=UTC)},
    ]

    class _FakeResult:
        def __iter__(self) -> object:
            return iter(fake_rows)

    class _FakeQueryJob:
        def result(self) -> _FakeResult:
            return _FakeResult()

    fake_client = mock.Mock()
    fake_client.query.return_value = _FakeQueryJob()

    out = tmp_path / "rows.jsonl"
    count = orchestrate._extract_query_to_jsonl(fake_client, "SELECT 1", [], out)

    assert count == 2
    lines = out.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"a": 1, "b": "x", "ts": "2026-05-11T00:00:00+00:00"}
    assert json.loads(lines[1]) == {"a": 2, "b": "y", "ts": "2026-05-12T00:00:00+00:00"}


def test_main_rejects_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPH_JOB", "make-coffee")
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCP_BQ_DATASET_SILVER", "s")

    with mock.patch.object(orchestrate.bigquery, "Client") as m:
        m.return_value = mock.Mock()
        with pytest.raises(SystemExit, match="unknown GRAPH_JOB"):
            orchestrate.main()


def test_main_requires_graph_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAPH_JOB", raising=False)
    with pytest.raises(SystemExit, match="GRAPH_JOB"):
        orchestrate.main()


class TestDetectCommunitiesV3:
    """_detect_communities passes v3 flags and loads extra tables when enabled."""

    def _make_fake_client(self, n_acts: int = 2, n_communities: int = 1) -> mock.Mock:
        class _Result:
            def __init__(self, rows: list) -> None:
                self._rows = rows

            def __iter__(self) -> object:
                return iter(self._rows)

        class _FakeQueryJob:
            def __init__(self, rows: list) -> None:
                self._rows = rows

            def result(self) -> _Result:
                return _Result(self._rows)

        act_rows = [{"author_id": f"a{i}", "platform": "x"} for i in range(n_acts)]
        client = mock.Mock()
        client.query.return_value = _FakeQueryJob(act_rows)
        return client

    def _patch_subprocess(self, tmp_dir: Path, *, emit_scores: bool, emit_series: bool):
        """Return a side_effect that writes fake JSONL outputs from the CLI."""

        def _run(cmd: list[str], *, check: bool) -> None:
            # Write communities file (always).
            out_idx = cmd.index("--out") + 1
            Path(cmd[out_idx]).write_text('{"community_id": 1}\n', encoding="utf-8")
            # Optionally write scores/series.
            if "--scores-out" in cmd:
                scores_idx = cmd.index("--scores-out") + 1
                if emit_scores:
                    Path(cmd[scores_idx]).write_text(
                        '{"community_id": 1}\n', encoding="utf-8"
                    )
            if "--series-out" in cmd:
                series_idx = cmd.index("--series-out") + 1
                if emit_series:
                    Path(cmd[series_idx]).write_text(
                        '{"series_id": "abc"}\n', encoding="utf-8"
                    )

        return _run

    def test_v3_flags_added_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MAPEAR_CIB_V3_SCORES_ENABLED", "true")
        monkeypatch.delenv("MAPEAR_CIB_USE_PERSONAS", raising=False)

        client = self._make_fake_client()
        cmds_seen: list[list[str]] = []

        def _fake_run(cmd: list[str], *, check: bool) -> None:
            cmds_seen.append(cmd)
            out_idx = cmd.index("--out") + 1
            Path(cmd[out_idx]).write_text('{"community_id": 1}\n', encoding="utf-8")
            if "--scores-out" in cmd:
                Path(cmd[cmd.index("--scores-out") + 1]).write_text(
                    '{"community_id": 1}\n', encoding="utf-8"
                )
            if "--series-out" in cmd:
                Path(cmd[cmd.index("--series-out") + 1]).write_text(
                    '{"series_id": "x"}\n', encoding="utf-8"
                )

        with (
            mock.patch.object(orchestrate.subprocess, "run", side_effect=_fake_run),
            mock.patch.object(orchestrate, "_load_jsonl_to_bq", return_value=1),
        ):
            orchestrate._detect_communities(client, "proj", "silver", "rn", None)

        assert len(cmds_seen) == 1
        cmd = cmds_seen[0]
        assert "--scores-out" in cmd
        assert "--series-out" in cmd

    def test_v3_skipped_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAPEAR_CIB_V3_SCORES_ENABLED", "false")
        monkeypatch.delenv("MAPEAR_CIB_USE_PERSONAS", raising=False)

        client = self._make_fake_client()
        cmds_seen: list[list[str]] = []

        def _fake_run(cmd: list[str], *, check: bool) -> None:
            cmds_seen.append(cmd)
            Path(cmd[cmd.index("--out") + 1]).write_text(
                '{"community_id": 1}\n', encoding="utf-8"
            )

        with (
            mock.patch.object(orchestrate.subprocess, "run", side_effect=_fake_run),
            mock.patch.object(orchestrate, "_load_jsonl_to_bq", return_value=1),
        ):
            orchestrate._detect_communities(client, "proj", "silver", "rn", None)

        cmd = cmds_seen[0]
        assert "--scores-out" not in cmd
        assert "--series-out" not in cmd

    def test_v3_loads_scores_and_series_tables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAPEAR_CIB_V3_SCORES_ENABLED", "true")
        monkeypatch.delenv("MAPEAR_CIB_USE_PERSONAS", raising=False)

        client = self._make_fake_client()
        loaded_tables: list[str] = []

        def _fake_run(cmd: list[str], *, check: bool) -> None:
            Path(cmd[cmd.index("--out") + 1]).write_text("{}\n", encoding="utf-8")
            Path(cmd[cmd.index("--scores-out") + 1]).write_text(
                "{}\n", encoding="utf-8"
            )
            Path(cmd[cmd.index("--series-out") + 1]).write_text(
                "{}\n", encoding="utf-8"
            )

        def _fake_load(
            client: object, path: Path, table_id: str, schema_table: str
        ) -> int:
            loaded_tables.append(table_id)
            return 1

        with (
            mock.patch.object(orchestrate.subprocess, "run", side_effect=_fake_run),
            mock.patch.object(orchestrate, "_load_jsonl_to_bq", side_effect=_fake_load),
        ):
            orchestrate._detect_communities(client, "proj", "silver", "rn", None)

        assert any("silver_author_communities" in t for t in loaded_tables)
        assert any("silver_community_scores" in t for t in loaded_tables)
        assert any("silver_cluster_series" in t for t in loaded_tables)

    def test_v3_skips_load_when_files_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAPEAR_CIB_V3_SCORES_ENABLED", "true")
        monkeypatch.delenv("MAPEAR_CIB_USE_PERSONAS", raising=False)

        client = self._make_fake_client()
        loaded_tables: list[str] = []

        def _fake_run(cmd: list[str], *, check: bool) -> None:
            # communities file has content; scores/series are empty (not written).
            Path(cmd[cmd.index("--out") + 1]).write_text("{}\n", encoding="utf-8")

        def _fake_load(
            client: object, path: Path, table_id: str, schema_table: str
        ) -> int:
            loaded_tables.append(table_id)
            return 1

        with (
            mock.patch.object(orchestrate.subprocess, "run", side_effect=_fake_run),
            mock.patch.object(orchestrate, "_load_jsonl_to_bq", side_effect=_fake_load),
        ):
            orchestrate._detect_communities(client, "proj", "silver", "rn", None)

        assert any("silver_author_communities" in t for t in loaded_tables)
        assert not any("silver_community_scores" in t for t in loaded_tables)
        assert not any("silver_cluster_series" in t for t in loaded_tables)

    def test_v3_embeddings_flag_added_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--embeddings is passed to the CLI when MAPEAR_CIB_V3_EMBEDDINGS_ENABLED=true."""
        monkeypatch.setenv("MAPEAR_CIB_V3_EMBEDDINGS_ENABLED", "true")
        monkeypatch.delenv("MAPEAR_CIB_V3_SCORES_ENABLED", raising=False)
        monkeypatch.delenv("MAPEAR_CIB_USE_PERSONAS", raising=False)

        # Return 2 activation rows + 1 embedding row from the two queries.
        act_rows = [
            {"author_id": "a0", "platform": "x"},
            {"author_id": "a1", "platform": "x"},
        ]
        emb_rows = [{"content_hash": "h1", "embedding": [0.1, 0.2]}]
        call_count = [0]

        class _FakeResult:
            def __init__(self, rows: list) -> None:
                self._rows = rows

            def __iter__(self) -> object:
                return iter(self._rows)

        class _FakeQueryJob:
            def __init__(self, rows: list) -> None:
                self._rows = rows

            def result(self) -> _FakeResult:
                return _FakeResult(self._rows)

        def _fake_query(query: str, job_config: object) -> _FakeQueryJob:
            call_count[0] += 1
            # First call: activations; second: embeddings.
            return _FakeQueryJob(act_rows if call_count[0] == 1 else emb_rows)

        client = mock.Mock()
        client.query.side_effect = _fake_query

        cmds_seen: list[list[str]] = []

        def _fake_run(cmd: list[str], *, check: bool) -> None:
            cmds_seen.append(cmd)
            Path(cmd[cmd.index("--out") + 1]).write_text("{}\n", encoding="utf-8")

        with (
            mock.patch.object(orchestrate.subprocess, "run", side_effect=_fake_run),
            mock.patch.object(orchestrate, "_load_jsonl_to_bq", return_value=1),
        ):
            orchestrate._detect_communities(client, "proj", "silver", "rn", None)

        assert len(cmds_seen) == 1
        assert "--embeddings" in cmds_seen[0]

    def test_v3_embeddings_skipped_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--embeddings is NOT passed when MAPEAR_CIB_V3_EMBEDDINGS_ENABLED is off."""
        monkeypatch.setenv("MAPEAR_CIB_V3_EMBEDDINGS_ENABLED", "false")
        monkeypatch.delenv("MAPEAR_CIB_V3_SCORES_ENABLED", raising=False)
        monkeypatch.delenv("MAPEAR_CIB_USE_PERSONAS", raising=False)

        client = self._make_fake_client()
        cmds_seen: list[list[str]] = []

        def _fake_run(cmd: list[str], *, check: bool) -> None:
            cmds_seen.append(cmd)
            Path(cmd[cmd.index("--out") + 1]).write_text("{}\n", encoding="utf-8")

        with (
            mock.patch.object(orchestrate.subprocess, "run", side_effect=_fake_run),
            mock.patch.object(orchestrate, "_load_jsonl_to_bq", return_value=1),
        ):
            orchestrate._detect_communities(client, "proj", "silver", "rn", None)

        assert "--embeddings" not in cmds_seen[0]
