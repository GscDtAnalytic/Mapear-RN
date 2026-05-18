"""Integration smoke tests for the social post embedding job — Eixo 2 v2a social.

Exercises run() end-to-end with a stub embedding client (no model loaded,
no GCS, no BQ). The same stub pattern as test_narrative_clustering_job.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from mapear_nlp.graph.run_social_embedding import run


class _StubClient:
    """Returns fixed-dim vectors derived from the first char of the text."""

    model = "stub-model-v0"
    dim = 4

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            first = ord(text[0]) if text else 65
            out.append([float(first), 0.0, 0.0, 0.0])
        return out


def _write_posts(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


_BASE_POST = {
    "content_hash": "abc123",
    "text": "Fátima Bezerra anuncia investimento.",
    "published_at": "2026-05-01T10:00:00Z",
    "region": "rn",
    "tenant_id": None,
    "platform": "facebook",
}


def test_run_emits_one_row_per_post(tmp_path: Path) -> None:
    posts = [
        {**_BASE_POST, "content_hash": "h1", "text": "Alpha post."},
        {**_BASE_POST, "content_hash": "h2", "text": "Beta post."},
        {**_BASE_POST, "content_hash": "h3", "text": "Gamma post."},
    ]
    posts_path = tmp_path / "posts.jsonl"
    out_path = tmp_path / "embeddings.jsonl"
    _write_posts(posts_path, posts)

    n = run(
        posts_path,
        out_path,
        region_filter=None,
        pipeline_version="0.1.0",
        embedding_client=_StubClient(),
        cache_enabled=False,
    )

    assert n == 3
    rows = _read_jsonl(out_path)
    assert len(rows) == 3
    hashes = {r["content_hash"] for r in rows}
    assert hashes == {"h1", "h2", "h3"}


def test_run_row_shape(tmp_path: Path) -> None:
    posts_path = tmp_path / "posts.jsonl"
    out_path = tmp_path / "embeddings.jsonl"
    _write_posts(posts_path, [_BASE_POST])

    run(
        posts_path,
        out_path,
        region_filter=None,
        pipeline_version="test-v1",
        embedding_client=_StubClient(),
        cache_enabled=False,
    )

    row = _read_jsonl(out_path)[0]
    assert row["content_hash"] == _BASE_POST["content_hash"]
    assert row["embedding_model"] == "stub-model-v0"
    assert row["embedding_dim"] == 4
    assert isinstance(row["embedding"], list)
    assert len(row["embedding"]) == 4
    assert row["source_type"] == "social"
    assert row["region"] == "rn"
    assert row["pipeline_version"] == "test-v1"
    assert row["schema_version"] == 1


def test_run_filters_by_region(tmp_path: Path) -> None:
    posts = [
        {**_BASE_POST, "content_hash": "h1", "region": "rn"},
        {**_BASE_POST, "content_hash": "h2", "region": "sp"},
    ]
    posts_path = tmp_path / "posts.jsonl"
    out_path = tmp_path / "embeddings.jsonl"
    _write_posts(posts_path, posts)

    n = run(
        posts_path,
        out_path,
        region_filter="rn",
        pipeline_version="0.1.0",
        embedding_client=_StubClient(),
        cache_enabled=False,
    )

    assert n == 1
    rows = _read_jsonl(out_path)
    assert rows[0]["content_hash"] == "h1"


def test_run_skips_empty_text(tmp_path: Path) -> None:
    posts = [
        {**_BASE_POST, "content_hash": "h1", "text": "Valid post."},
        {**_BASE_POST, "content_hash": "h2", "text": ""},
        {**_BASE_POST, "content_hash": "h3", "text": None},
    ]
    posts_path = tmp_path / "posts.jsonl"
    out_path = tmp_path / "embeddings.jsonl"
    _write_posts(posts_path, posts)

    n = run(
        posts_path,
        out_path,
        region_filter=None,
        pipeline_version="0.1.0",
        embedding_client=_StubClient(),
        cache_enabled=False,
    )

    assert n == 1
    rows = _read_jsonl(out_path)
    assert rows[0]["content_hash"] == "h1"


def test_run_deduplicates_by_content_hash(tmp_path: Path) -> None:
    posts = [
        {**_BASE_POST, "content_hash": "h1", "text": "Same post."},
        {**_BASE_POST, "content_hash": "h1", "text": "Same post."},
        {**_BASE_POST, "content_hash": "h2", "text": "Other post."},
    ]
    posts_path = tmp_path / "posts.jsonl"
    out_path = tmp_path / "embeddings.jsonl"
    _write_posts(posts_path, posts)

    n = run(
        posts_path,
        out_path,
        region_filter=None,
        pipeline_version="0.1.0",
        embedding_client=_StubClient(),
        cache_enabled=False,
    )

    assert n == 2
    hashes = {r["content_hash"] for r in _read_jsonl(out_path)}
    assert hashes == {"h1", "h2"}


def test_run_disabled_writes_nothing(tmp_path: Path) -> None:
    posts_path = tmp_path / "posts.jsonl"
    out_path = tmp_path / "embeddings.jsonl"
    _write_posts(posts_path, [_BASE_POST])

    n = run(
        posts_path,
        out_path,
        region_filter=None,
        pipeline_version="0.1.0",
        embedding_client=_StubClient(),
        cache_enabled=False,
        embedding_enabled=False,
    )

    assert n == 0
    assert not out_path.exists()


def test_run_empty_input_writes_empty_output(tmp_path: Path) -> None:
    posts_path = tmp_path / "posts.jsonl"
    out_path = tmp_path / "embeddings.jsonl"
    posts_path.write_text("")

    n = run(
        posts_path,
        out_path,
        region_filter=None,
        pipeline_version="0.1.0",
        embedding_client=_StubClient(),
        cache_enabled=False,
    )

    assert n == 0
    assert not out_path.exists()
