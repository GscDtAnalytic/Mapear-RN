"""Integration smoke tests for the narrative-clustering job — Eixo 2 v2a.

The job is exercised end-to-end (JSONL in → JSONL out) with a stub
embedding client and the cosine_threshold algorithm so the test runs
in milliseconds and never imports sentence-transformers / hdbscan.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from mapear_nlp.clustering.run_narrative_clustering import run


class _DirectionalClient:
    """Returns 8-dim unit vectors derived from a per-narrative angle.

    The angle is encoded in the first character of each narrative
    (position in alphabet), so narratives starting with the same letter
    cluster tightly and different starting letters land far apart. This
    makes the test deterministic and the asserts easy to reason about.
    """

    def __init__(self) -> None:
        self.model = "directional-stub"
        self.dim = 8

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            first = text.strip()[0].lower() if text.strip() else "a"
            angle_deg = (ord(first) - ord("a")) * 18.0
            a = math.radians(angle_deg)
            out.append([math.cos(a), math.sin(a), 0, 0, 0, 0, 0, 0])
        return out


def _write_gold(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_job_emits_embeddings_and_clusters_for_one_day(tmp_path: Path) -> None:
    """Three articles starting with 'a' → one cluster + 3 embedding rows."""
    gold_path = tmp_path / "gold.jsonl"
    emb_path = tmp_path / "embeddings.jsonl"
    cluster_path = tmp_path / "clusters.jsonl"
    _write_gold(
        gold_path,
        [
            {
                "content_hash": "h1",
                "narrative_summary": "alpha frame on healthcare cuts.",
                "published_at": "2026-04-01T10:00:00Z",
                "region": "rn",
                "tenant_id": None,
                "narrative_prompt_version": "narrative_v1",
                "rule_version": "v2.1",
                "source_type": "rss",
            },
            {
                "content_hash": "h2",
                "narrative_summary": "alpha framing on hospitals losing beds.",
                "published_at": "2026-04-01T11:00:00Z",
                "region": "rn",
                "tenant_id": None,
                "narrative_prompt_version": "narrative_v1",
                "rule_version": "v2.1",
                "source_type": "rss",
            },
            {
                "content_hash": "h3",
                "narrative_summary": "alpha take on the budget cut.",
                "published_at": "2026-04-01T12:00:00Z",
                "region": "rn",
                "tenant_id": None,
                "narrative_prompt_version": "narrative_v1",
                "rule_version": "v2.1",
                "source_type": "rss",
            },
        ],
    )

    rc = run(
        gold_path=gold_path,
        embeddings_out=emb_path,
        clusters_out=cluster_path,
        algorithm="cosine_threshold",
        min_size=3,
        cosine_threshold=0.95,
        region_filter=None,
        pipeline_version="0.1.0",
        embedding_client=_DirectionalClient(),
        cache_enabled=False,
    )
    assert rc == 0

    embeddings = _read_jsonl(emb_path)
    assert len(embeddings) == 3
    assert {row["content_hash"] for row in embeddings} == {"h1", "h2", "h3"}
    assert all(row["embedding_model"] == "directional-stub" for row in embeddings)
    assert all(row["embedding_dim"] == 8 for row in embeddings)
    assert all(len(row["embedding"]) == 8 for row in embeddings)
    assert all(row["region"] == "rn" for row in embeddings)
    assert all(row["narrative_prompt_version"] == "narrative_v1" for row in embeddings)

    clusters = _read_jsonl(cluster_path)
    assert len(clusters) == 3
    assert {row["cluster_id"] for row in clusters} == {0}
    assert {row["member_role"] for row in clusters} == {"centroid", "member"}
    assert all(row["cluster_size"] == 3 for row in clusters)
    assert all(row["algorithm"] == "cosine_threshold" for row in clusters)


def test_job_skips_rows_without_narrative_summary(tmp_path: Path) -> None:
    """Rows with NULL narrative_summary are silently skipped (non-ALERT)."""
    gold_path = tmp_path / "gold.jsonl"
    emb_path = tmp_path / "embeddings.jsonl"
    cluster_path = tmp_path / "clusters.jsonl"
    _write_gold(
        gold_path,
        [
            {
                "content_hash": "h1",
                "narrative_summary": "alpha cluster.",
                "published_at": "2026-04-01T10:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "h2",
                "narrative_summary": None,  # WARNING / FAVORABLE → skip
                "published_at": "2026-04-01T11:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "h3",
                "narrative_summary": "",  # empty → skip
                "published_at": "2026-04-01T12:00:00Z",
                "region": "rn",
            },
        ],
    )

    run(
        gold_path=gold_path,
        embeddings_out=emb_path,
        clusters_out=cluster_path,
        algorithm="cosine_threshold",
        min_size=2,
        cosine_threshold=0.95,
        region_filter=None,
        pipeline_version="0.1.0",
        embedding_client=_DirectionalClient(),
        cache_enabled=False,
    )

    embeddings = _read_jsonl(emb_path)
    assert len(embeddings) == 1
    assert embeddings[0]["content_hash"] == "h1"


def test_job_filters_by_region(tmp_path: Path) -> None:
    """--region rn drops pe rows."""
    gold_path = tmp_path / "gold.jsonl"
    emb_path = tmp_path / "embeddings.jsonl"
    cluster_path = tmp_path / "clusters.jsonl"
    _write_gold(
        gold_path,
        [
            {
                "content_hash": "rn1",
                "narrative_summary": "alpha.",
                "published_at": "2026-04-01T10:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "rn2",
                "narrative_summary": "alpha alpha.",
                "published_at": "2026-04-01T11:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "pe1",
                "narrative_summary": "alpha PE.",
                "published_at": "2026-04-01T12:00:00Z",
                "region": "pe",
            },
        ],
    )

    run(
        gold_path=gold_path,
        embeddings_out=emb_path,
        clusters_out=cluster_path,
        algorithm="cosine_threshold",
        min_size=2,
        cosine_threshold=0.95,
        region_filter="rn",
        pipeline_version="0.1.0",
        embedding_client=_DirectionalClient(),
        cache_enabled=False,
    )

    embeddings = _read_jsonl(emb_path)
    assert {row["content_hash"] for row in embeddings} == {"rn1", "rn2"}
    assert all(row["region"] == "rn" for row in embeddings)


def test_job_emits_outliers_with_negative_cluster_id(tmp_path: Path) -> None:
    """A lonely narrative + a 2-narrative cluster (below min_size=3) → all outliers."""
    gold_path = tmp_path / "gold.jsonl"
    emb_path = tmp_path / "embeddings.jsonl"
    cluster_path = tmp_path / "clusters.jsonl"
    _write_gold(
        gold_path,
        [
            {
                "content_hash": "a1",
                "narrative_summary": "alpha frame.",
                "published_at": "2026-04-01T10:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "a2",
                "narrative_summary": "alpha take.",
                "published_at": "2026-04-01T11:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "z1",
                "narrative_summary": "zeta lone narrative.",
                "published_at": "2026-04-01T12:00:00Z",
                "region": "rn",
            },
        ],
    )

    run(
        gold_path=gold_path,
        embeddings_out=emb_path,
        clusters_out=cluster_path,
        algorithm="cosine_threshold",
        min_size=3,
        cosine_threshold=0.95,
        region_filter=None,
        pipeline_version="0.1.0",
        embedding_client=_DirectionalClient(),
        cache_enabled=False,
    )

    clusters = _read_jsonl(cluster_path)
    assert len(clusters) == 3
    # All below min_size=3, so everyone is an outlier.
    assert all(row["cluster_id"] == -1 for row in clusters)
    assert all(row["member_role"] == "outlier" for row in clusters)


def test_job_groups_by_day_emitting_separate_clusters(tmp_path: Path) -> None:
    """Same alpha narratives on two different days → two separate clusters."""
    gold_path = tmp_path / "gold.jsonl"
    emb_path = tmp_path / "embeddings.jsonl"
    cluster_path = tmp_path / "clusters.jsonl"
    _write_gold(
        gold_path,
        [
            # Day 1
            {
                "content_hash": "d1_a",
                "narrative_summary": "alpha 1.",
                "published_at": "2026-04-01T10:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "d1_b",
                "narrative_summary": "alpha 2.",
                "published_at": "2026-04-01T11:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "d1_c",
                "narrative_summary": "alpha 3.",
                "published_at": "2026-04-01T12:00:00Z",
                "region": "rn",
            },
            # Day 2 — same framing, different day
            {
                "content_hash": "d2_a",
                "narrative_summary": "alpha 4.",
                "published_at": "2026-04-02T10:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "d2_b",
                "narrative_summary": "alpha 5.",
                "published_at": "2026-04-02T11:00:00Z",
                "region": "rn",
            },
            {
                "content_hash": "d2_c",
                "narrative_summary": "alpha 6.",
                "published_at": "2026-04-02T12:00:00Z",
                "region": "rn",
            },
        ],
    )

    run(
        gold_path=gold_path,
        embeddings_out=emb_path,
        clusters_out=cluster_path,
        algorithm="cosine_threshold",
        min_size=3,
        cosine_threshold=0.95,
        region_filter=None,
        pipeline_version="0.1.0",
        embedding_client=_DirectionalClient(),
        cache_enabled=False,
    )

    clusters = _read_jsonl(cluster_path)
    by_day = {}
    for row in clusters:
        day = row["cluster_run_date"][:10]
        by_day.setdefault(day, []).append(row)
    assert set(by_day) == {"2026-04-01", "2026-04-02"}
    # Each day forms its own cluster (id=0 within the day).
    for _day, rows in by_day.items():
        assert len({row["cluster_id"] for row in rows}) == 1
        assert rows[0]["cluster_size"] == 3
