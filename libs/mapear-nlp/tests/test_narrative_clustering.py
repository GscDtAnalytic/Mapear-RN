"""Unit tests for narrative clustering — Eixo 2 v2a.

The cosine_threshold algorithm is pure-Python and always runs. HDBSCAN
tests skip when the optional ``hdbscan`` dep is missing.
"""

from __future__ import annotations

import math

import pytest

from mapear_nlp.clustering.narrative import (
    ClusteringResult,
    NarrativeCluster,
    NarrativeClusterAssignment,
    compute_narrative_clusters,
)


def _unit(vec: list[float]) -> list[float]:
    """Normalise to unit length so cosine similarity == dot product."""
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec]


def _vec(angle_deg: float) -> list[float]:
    """A 2D unit vector at the given angle, padded to 8 dims with zeros."""
    a = math.radians(angle_deg)
    return [math.cos(a), math.sin(a), 0, 0, 0, 0, 0, 0]


def test_empty_input_yields_empty_result() -> None:
    result = compute_narrative_clusters([], algorithm="cosine_threshold")
    assert result.assignments == []
    assert result.clusters == []


def test_cosine_threshold_clusters_tight_group() -> None:
    """3 narratives pointing in nearly the same direction → one cluster."""
    items = [
        ("h1", _vec(0)),
        ("h2", _vec(5)),
        ("h3", _vec(10)),
    ]
    result = compute_narrative_clusters(
        items, algorithm="cosine_threshold", cosine_threshold=0.95, min_size=3
    )
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert cluster.cluster_size == 3
    assert cluster.members == ("h1", "h2", "h3")
    assert cluster.algorithm == "cosine_threshold"
    # Every input has an assignment, none is outlier.
    assert {a.cluster_id for a in result.assignments} == {0}
    roles = {a.member_role for a in result.assignments}
    assert roles == {"centroid", "member"}


def test_cosine_threshold_splits_disjoint_groups() -> None:
    """Two clusters 90° apart → two separate clusters."""
    items = [
        ("g1_a", _vec(0)),
        ("g1_b", _vec(5)),
        ("g1_c", _vec(10)),
        ("g2_a", _vec(90)),
        ("g2_b", _vec(95)),
        ("g2_c", _vec(100)),
    ]
    result = compute_narrative_clusters(
        items, algorithm="cosine_threshold", cosine_threshold=0.95, min_size=3
    )
    assert len(result.clusters) == 2
    member_sets = {c.members for c in result.clusters}
    assert ("g1_a", "g1_b", "g1_c") in member_sets
    assert ("g2_a", "g2_b", "g2_c") in member_sets


def test_outliers_below_min_size_marked_as_outlier() -> None:
    """A 3-narrative cluster + 1 lonely narrative → cluster + outlier row."""
    items = [
        ("h1", _vec(0)),
        ("h2", _vec(5)),
        ("h3", _vec(10)),
        ("loner", _vec(170)),  # far from the cluster
    ]
    result = compute_narrative_clusters(
        items, algorithm="cosine_threshold", cosine_threshold=0.95, min_size=3
    )
    assert len(result.clusters) == 1
    outliers = [a for a in result.assignments if a.cluster_id == -1]
    assert len(outliers) == 1
    assert outliers[0].content_hash == "loner"
    assert outliers[0].member_role == "outlier"
    assert outliers[0].distance_to_centroid is None


def test_min_size_filters_small_clusters() -> None:
    """A pair below min_size=3 becomes outliers, not a cluster."""
    items = [
        ("h1", _vec(0)),
        ("h2", _vec(2)),
    ]
    result = compute_narrative_clusters(
        items, algorithm="cosine_threshold", cosine_threshold=0.95, min_size=3
    )
    assert result.clusters == []
    assert all(a.cluster_id == -1 for a in result.assignments)
    assert all(a.member_role == "outlier" for a in result.assignments)


def test_cluster_id_deterministic_under_input_reorder() -> None:
    """Reordering inputs preserves cluster IDs (sorted-membership rule)."""
    items_a = [
        ("g1_a", _vec(0)),
        ("g1_b", _vec(5)),
        ("g1_c", _vec(10)),
        ("g2_a", _vec(90)),
        ("g2_b", _vec(95)),
        ("g2_c", _vec(100)),
    ]
    items_b = list(reversed(items_a))
    result_a = compute_narrative_clusters(items_a, algorithm="cosine_threshold")
    result_b = compute_narrative_clusters(items_b, algorithm="cosine_threshold")
    # Same clusters, same IDs regardless of input order.
    ids_a = {c.cluster_id: c.members for c in result_a.clusters}
    ids_b = {c.cluster_id: c.members for c in result_b.clusters}
    assert ids_a == ids_b


def test_centroid_member_is_closest_to_centroid() -> None:
    """The 'centroid' role marks the narrative nearest the cluster centroid."""
    items = [
        ("h1", _vec(0)),
        ("h2", _vec(5)),  # closest to the centroid of (0, 5, 10) = 5
        ("h3", _vec(10)),
    ]
    result = compute_narrative_clusters(
        items, algorithm="cosine_threshold", cosine_threshold=0.95, min_size=3
    )
    centroid = [a for a in result.assignments if a.member_role == "centroid"]
    assert len(centroid) == 1
    assert centroid[0].content_hash == "h2"


def test_mixed_embedding_dims_raises() -> None:
    items = [
        ("h1", [1.0, 0.0]),
        ("h2", [1.0, 0.0, 0.0]),
    ]
    with pytest.raises(ValueError, match="mixed embedding dims"):
        compute_narrative_clusters(items, algorithm="cosine_threshold")


def test_min_size_below_two_raises() -> None:
    with pytest.raises(ValueError, match="min_size must be >= 2"):
        compute_narrative_clusters(
            [("h1", _vec(0))], algorithm="cosine_threshold", min_size=1
        )


def test_unknown_algorithm_raises() -> None:
    with pytest.raises(ValueError, match="unknown algorithm"):
        compute_narrative_clusters(
            [("h1", _vec(0))],
            algorithm="not_an_algorithm",  # type: ignore[arg-type]
        )


# --- HDBSCAN path — skipped per-test when the optional dep is missing -------


def _require_hdbscan() -> None:
    pytest.importorskip("hdbscan")
    pytest.importorskip("numpy")


def test_hdbscan_clusters_two_tight_groups() -> None:
    _require_hdbscan()
    """Two well-separated 5-narrative clusters in unit space."""
    items: list[tuple[str, list[float]]] = []
    # Cluster A around (1, 0, ...)
    for i in range(5):
        items.append((f"a{i}", _unit([1.0 + i * 0.001, 0.001 * i] + [0] * 6)))
    # Cluster B around (0, 1, ...)
    for i in range(5):
        items.append((f"b{i}", _unit([0.001 * i, 1.0 + i * 0.001] + [0] * 6)))

    result = compute_narrative_clusters(items, algorithm="hdbscan", min_size=3)
    assert len(result.clusters) == 2
    sizes = sorted(c.cluster_size for c in result.clusters)
    assert sizes == [5, 5]


def test_hdbscan_marks_lonely_narrative_as_outlier() -> None:
    _require_hdbscan()
    items: list[tuple[str, list[float]]] = []
    for i in range(5):
        items.append((f"a{i}", _unit([1.0 + i * 0.001, 0.001 * i] + [0] * 6)))
    items.append(("loner", _unit([0.0, 1.0] + [0] * 6)))
    result = compute_narrative_clusters(items, algorithm="hdbscan", min_size=3)
    outliers = [a for a in result.assignments if a.cluster_id == -1]
    assert any(a.content_hash == "loner" for a in outliers)


def test_hdbscan_deterministic_across_runs() -> None:
    _require_hdbscan()
    items = [
        ("h0", _unit([1.0, 0.01] + [0] * 6)),
        ("h1", _unit([1.0, 0.02] + [0] * 6)),
        ("h2", _unit([1.0, 0.03] + [0] * 6)),
        ("h3", _unit([1.0, 0.04] + [0] * 6)),
        ("h4", _unit([1.0, 0.05] + [0] * 6)),
    ]
    r1 = compute_narrative_clusters(items, algorithm="hdbscan", min_size=3)
    r2 = compute_narrative_clusters(items, algorithm="hdbscan", min_size=3)
    assert [c.members for c in r1.clusters] == [c.members for c in r2.clusters]


def test_cluster_result_dataclasses_are_frozen() -> None:
    # Frozen dataclasses raise FrozenInstanceError on attribute set.
    from dataclasses import FrozenInstanceError

    assignment = NarrativeClusterAssignment(
        content_hash="h", cluster_id=0, member_role="member", distance_to_centroid=0.1
    )
    with pytest.raises(FrozenInstanceError):
        assignment.cluster_id = 1  # type: ignore[misc]

    cluster = NarrativeCluster(
        cluster_id=0,
        members=("h",),
        centroid=(1.0,),
        cluster_size=1,
        avg_intra_cluster_distance=0.0,
        algorithm="hdbscan",
    )
    with pytest.raises(FrozenInstanceError):
        cluster.cluster_id = 1  # type: ignore[misc]

    result = ClusteringResult(assignments=[], clusters=[])
    with pytest.raises(FrozenInstanceError):
        result.assignments = [assignment]  # type: ignore[misc]
