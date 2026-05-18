"""Integration tests for the out-of-band author-resolution job — Eixo 3 v2b."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mapear_nlp.graph.run_author_resolution import run


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


@pytest.fixture
def tmp_authors(tmp_path: Path) -> Path:
    path = tmp_path / "authors.jsonl"
    _write_jsonl(
        path,
        [
            {
                "platform": "facebook",
                "author_id": "zoey",
                "display_name": "Zoey",
                "region": "rn",
                "tenant_id": "default",
            },
            {
                "platform": "instagram",
                "author_id": "zoey",
                "display_name": "Zoey",
                "region": "rn",
                "tenant_id": "default",
            },
            {
                "platform": "x",
                "author_id": "solo",
                "display_name": "Solo",
                "region": "rn",
                "tenant_id": "default",
            },
        ],
    )
    return path


def test_job_emits_persona_rows(tmp_path: Path, tmp_authors: Path) -> None:
    out = tmp_path / "personas.jsonl"
    rc = run(
        authors_path=tmp_authors,
        out_path=out,
        handle_similarity=0.90,
        display_name_similarity=0.90,
        min_shared_content=1,
        use_content_hash_bridge=True,
        region_filter=None,
        pipeline_version="test",
        audit_enabled=False,
    )
    assert rc == 0
    rows = _read_jsonl(out)
    # One 2-member persona → two rows. The solo author is not emitted.
    assert len(rows) == 2
    persona_ids = {r["persona_id"] for r in rows}
    assert len(persona_ids) == 1
    assert {r["platform"] for r in rows} == {"facebook", "instagram"}
    for row in rows:
        assert row["member_count"] == 2
        assert row["region"] == "rn"
        assert row["tenant_id"] == "default"
        assert row["resolution_version"]
        # evidence_json round-trips as a list of pair score dicts.
        evidence = json.loads(row["evidence_json"])
        assert evidence
        assert evidence[0]["decision"] == "match"


def test_job_region_filter(tmp_path: Path) -> None:
    authors = tmp_path / "authors.jsonl"
    _write_jsonl(
        authors,
        [
            {
                "platform": "facebook",
                "author_id": "zoey",
                "display_name": "Zoey",
                "region": "rn",
            },
            {
                "platform": "instagram",
                "author_id": "zoey",
                "display_name": "Zoey",
                "region": "rn",
            },
            {
                "platform": "facebook",
                "author_id": "carla",
                "display_name": "Carla",
                "region": "pe",
            },
            {
                "platform": "instagram",
                "author_id": "carla",
                "display_name": "Carla",
                "region": "pe",
            },
        ],
    )
    out = tmp_path / "rn_only.jsonl"
    rc = run(
        authors_path=authors,
        out_path=out,
        handle_similarity=0.90,
        display_name_similarity=0.90,
        min_shared_content=1,
        use_content_hash_bridge=True,
        region_filter="rn",
        pipeline_version="test",
        audit_enabled=False,
    )
    assert rc == 0
    rows = _read_jsonl(out)
    assert all(r["region"] == "rn" for r in rows)
    assert {r["author_id"] for r in rows} == {"zoey"}


def test_job_no_authors_after_filter_returns_zero(
    tmp_path: Path, tmp_authors: Path
) -> None:
    out = tmp_path / "empty.jsonl"
    rc = run(
        authors_path=tmp_authors,
        out_path=out,
        handle_similarity=0.90,
        display_name_similarity=0.90,
        min_shared_content=1,
        use_content_hash_bridge=True,
        region_filter="nonexistent",
        pipeline_version="test",
        audit_enabled=False,
    )
    assert rc == 0
    assert not out.exists() or out.read_text() == ""


def test_job_run_at_and_activation_date_are_iso(
    tmp_path: Path, tmp_authors: Path
) -> None:
    out = tmp_path / "personas.jsonl"
    run(
        authors_path=tmp_authors,
        out_path=out,
        handle_similarity=0.90,
        display_name_similarity=0.90,
        min_shared_content=1,
        use_content_hash_bridge=True,
        region_filter=None,
        pipeline_version="test",
        audit_enabled=False,
    )
    rows = _read_jsonl(out)
    assert rows
    # ISO strings parse — the activation_date is the day boundary.
    from datetime import datetime as dt

    parsed = dt.fromisoformat(rows[0]["activation_date"])
    assert parsed.hour == parsed.minute == parsed.second == 0
