"""Unit tests for community detection — Eixo 3 v2a."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("networkx")

from mapear_nlp.graph.coactivation import (  # noqa: E402
    AuthorKey,
    AuthorPair,
)
from mapear_nlp.graph.community import (  # noqa: E402
    build_graph,
    detect_communities,
)


def _pair(
    a: str,
    b: str,
    *,
    platform: str = "x",
    co_post_count: int = 5,
    jaccard: float = 1.0,
    targets: tuple[str, ...] = ("t",),
) -> AuthorPair:
    ka = AuthorKey(platform, a)
    kb = AuthorKey(platform, b)
    if ka > kb:
        ka, kb = kb, ka
    return AuthorPair(
        author_a=ka,
        author_b=kb,
        co_post_count=co_post_count,
        shared_targets=targets,
        jaccard=jaccard,
        first_seen_at=datetime(2026, 5, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 5, 2, tzinfo=UTC),
    )


def test_empty_input_yields_empty_communities() -> None:
    assert detect_communities(build_graph([])) == []


def test_single_triangle_is_one_community() -> None:
    pairs = [_pair("a", "b"), _pair("b", "c"), _pair("a", "c")]
    communities = detect_communities(build_graph(pairs), min_size=3)
    assert len(communities) == 1
    members = communities[0].members
    assert {m.author_id for m in members} == {"a", "b", "c"}
    assert communities[0].edge_count == 3
    assert communities[0].edge_density == pytest.approx(1.0)


def test_two_disconnected_triangles_yield_two_communities() -> None:
    pairs = [
        _pair("a", "b"),
        _pair("b", "c"),
        _pair("a", "c"),
        _pair("x", "y"),
        _pair("y", "z"),
        _pair("x", "z"),
    ]
    communities = detect_communities(build_graph(pairs), min_size=3)
    assert len(communities) == 2
    sizes = {len(c.members) for c in communities}
    assert sizes == {3}
    # Communities are sorted by (size desc, members asc) — when sizes
    # tie, the alphabetically-first member set comes first.
    assert {m.author_id for m in communities[0].members} == {"a", "b", "c"}


def test_min_size_filters_small_communities() -> None:
    """A 2-node dyad is filtered out at default min_size=3."""
    pairs = [
        _pair("a", "b"),
        _pair("a", "b", co_post_count=10),  # duplicate edge — uses last weight
        _pair("x", "y"),
        _pair("y", "z"),
        _pair("x", "z"),
    ]
    communities = detect_communities(build_graph(pairs), min_size=3)
    assert len(communities) == 1
    assert {m.author_id for m in communities[0].members} == {"x", "y", "z"}


def test_louvain_is_deterministic_with_seed() -> None:
    """Same seed → same community IDs and membership."""
    pairs = [
        _pair("a", "b"),
        _pair("b", "c"),
        _pair("a", "c"),
        _pair("x", "y"),
        _pair("y", "z"),
        _pair("x", "z"),
    ]
    g = build_graph(pairs)
    run_a = detect_communities(g, algorithm="louvain", seed=42, min_size=3)
    run_b = detect_communities(g, algorithm="louvain", seed=42, min_size=3)
    assert run_a == run_b


def test_label_propagation_separates_clear_components() -> None:
    """Label propagation must find disconnected components verbatim."""
    pairs = [
        _pair("a", "b"),
        _pair("b", "c"),
        _pair("a", "c"),
        _pair("x", "y"),
        _pair("y", "z"),
        _pair("x", "z"),
    ]
    communities = detect_communities(
        build_graph(pairs), algorithm="label_propagation", min_size=3
    )
    assert len(communities) == 2
    member_sets = [{m.author_id for m in c.members} for c in communities]
    assert {"a", "b", "c"} in member_sets
    assert {"x", "y", "z"} in member_sets


def test_avg_weight_and_jaccard_are_averaged_per_community() -> None:
    pairs = [
        _pair("a", "b", co_post_count=10, jaccard=0.5),
        _pair("b", "c", co_post_count=20, jaccard=1.0),
        _pair("a", "c", co_post_count=30, jaccard=0.75),
    ]
    communities = detect_communities(build_graph(pairs), min_size=3)
    c = communities[0]
    assert c.avg_co_post_count == pytest.approx((10 + 20 + 30) / 3)
    assert c.avg_jaccard == pytest.approx((0.5 + 1.0 + 0.75) / 3)


def test_community_ids_are_deterministic_across_runs() -> None:
    """Community IDs depend on sorted membership, not algorithm iteration."""
    pairs = [
        _pair("ant_a", "ant_b"),
        _pair("ant_b", "ant_c"),
        _pair("ant_a", "ant_c"),
        _pair("xyz_a", "xyz_b"),
        _pair("xyz_b", "xyz_c"),
        _pair("xyz_a", "xyz_c"),
    ]
    communities = detect_communities(build_graph(pairs), min_size=3)
    # ant_* group is alphabetically first, gets community_id=0.
    assert communities[0].community_id == 0
    assert "ant_a" in {m.author_id for m in communities[0].members}
    assert communities[1].community_id == 1
    assert "xyz_a" in {m.author_id for m in communities[1].members}


def test_min_size_below_2_rejected() -> None:
    with pytest.raises(ValueError, match="min_size"):
        detect_communities(build_graph([_pair("a", "b")]), min_size=1)


def test_unknown_algorithm_rejected() -> None:
    pairs = [_pair("a", "b"), _pair("b", "c"), _pair("a", "c")]
    with pytest.raises(ValueError, match="unknown algorithm"):
        detect_communities(
            build_graph(pairs),
            algorithm="leiden",  # type: ignore[arg-type]
            min_size=3,
        )


def test_bridge_node_pulled_into_dominant_community() -> None:
    """A node connected to two cliques is assigned to the stronger one."""
    pairs = [
        # Heavy clique A — co_post_count=10
        _pair("a", "b", co_post_count=10),
        _pair("b", "c", co_post_count=10),
        _pair("a", "c", co_post_count=10),
        # Light bridge from b → x
        _pair("b", "x", co_post_count=1),
        # Heavy clique X — co_post_count=10
        _pair("x", "y", co_post_count=10),
        _pair("y", "z", co_post_count=10),
        _pair("x", "z", co_post_count=10),
    ]
    communities = detect_communities(
        build_graph(pairs), algorithm="louvain", seed=42, min_size=3
    )
    # Expect two communities; b stays with its heavy clique (a, b, c).
    assert len(communities) == 2
    abc = next(c for c in communities if "a" in {m.author_id for m in c.members})
    xyz = next(c for c in communities if "x" in {m.author_id for m in c.members})
    assert "b" in {m.author_id for m in abc.members}
    assert "y" in {m.author_id for m in xyz.members}


def test_algorithm_field_recorded_in_stats() -> None:
    pairs = [_pair("a", "b"), _pair("b", "c"), _pair("a", "c")]
    g = build_graph(pairs)
    assert detect_communities(g, algorithm="louvain")[0].algorithm == "louvain"
    assert (
        detect_communities(g, algorithm="label_propagation")[0].algorithm
        == "label_propagation"
    )
