"""Unit tests for the author co-activation engine — Eixo 3 v1."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mapear_nlp.graph.coactivation import (
    AuthorKey,
    compute_coactivation_scores,
)


def _row(
    author: str,
    target: str,
    *,
    minutes: float = 0.0,
    platform: str = "x",
    post_id: str | None = None,
) -> dict:
    return {
        "author_id": author,
        "platform": platform,
        "person_target": target,
        "published_at": datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
        + timedelta(minutes=minutes),
        "post_id": post_id or f"x:{author}:{int(minutes)}",
    }


def test_empty_input_yields_empty_pairs() -> None:
    assert compute_coactivation_scores([]) == []


def test_isolated_author_produces_no_pairs() -> None:
    rows = [
        _row("alice", "fatima", minutes=0),
        _row("alice", "fatima", minutes=60),
        _row("alice", "fatima", minutes=120),
    ]
    assert compute_coactivation_scores(rows, min_overlap=1) == []


def test_simple_pair_counts_one_co_post_per_window() -> None:
    rows = [
        _row("alice", "fatima", minutes=0),
        _row("bob", "fatima", minutes=10),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=1)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.author_a == AuthorKey("x", "alice")
    assert p.author_b == AuthorKey("x", "bob")
    assert p.co_post_count == 1
    assert p.shared_targets == ("fatima",)
    assert p.jaccard == 1.0


def test_window_boundary_excludes_late_partner() -> None:
    rows = [
        _row("alice", "fatima", minutes=0),
        _row("bob", "fatima", minutes=61),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=1)
    assert pairs == []


def test_min_overlap_filters_pairs_below_threshold() -> None:
    rows = [
        _row("alice", "fatima", minutes=0),
        _row("bob", "fatima", minutes=5),
        _row("alice", "fatima", minutes=200),
        _row("bob", "fatima", minutes=205),
    ]
    pairs_loose = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=1)
    pairs_strict = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=3)
    assert pairs_loose[0].co_post_count == 2
    assert pairs_strict == []


def test_same_author_multiple_posts_in_window_no_self_pair() -> None:
    """An author who posts twice inside the window does not pair with self."""
    rows = [
        _row("alice", "fatima", minutes=0),
        _row("alice", "fatima", minutes=5),
        _row("alice", "fatima", minutes=10),
        _row("bob", "fatima", minutes=15),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=1)
    assert len(pairs) == 1
    assert pairs[0].co_post_count == 1


def test_jaccard_uses_lifetime_target_sets() -> None:
    """Jaccard is over lifetime, not just window co-occurrences."""
    rows = [
        # Co-fire on fatima.
        _row("alice", "fatima", minutes=0),
        _row("bob", "fatima", minutes=5),
        # alice also activates on outros targets that bob never touches.
        _row("alice", "antenor", minutes=120),
        _row("alice", "rogerio", minutes=240),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=1)
    assert len(pairs) == 1
    p = pairs[0]
    # alice targets = {fatima, antenor, rogerio}, bob = {fatima}
    # intersection = {fatima}, union = {fatima, antenor, rogerio}
    assert p.jaccard == pytest.approx(1 / 3)
    assert p.shared_targets == ("fatima",)


def test_cross_platform_same_handle_is_two_distinct_authors() -> None:
    """v1 surrogate: same handle on FB and IG are two authors."""
    rows = [
        _row("zoey", "fatima", minutes=0, platform="facebook"),
        _row("zoey", "fatima", minutes=10, platform="instagram"),
        _row("zoey", "fatima", minutes=20, platform="facebook"),
        _row("zoey", "fatima", minutes=30, platform="instagram"),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=1)
    assert len(pairs) == 1
    # Should be (facebook, zoey) ↔ (instagram, zoey) — cross-platform
    # candidate; v2 will resolve into a single author.
    a, b = pairs[0].author_a, pairs[0].author_b
    assert {a.platform, b.platform} == {"facebook", "instagram"}
    assert a.author_id == b.author_id == "zoey"


def test_pair_keys_are_canonical_unordered() -> None:
    """No (a, b) AND (b, a) — sorted canonical key."""
    rows = [
        _row("zara", "fatima", minutes=0),
        _row("alice", "fatima", minutes=10),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=1)
    assert len(pairs) == 1
    assert pairs[0].author_a < pairs[0].author_b


def test_first_and_last_seen_track_window_anchors() -> None:
    rows = [
        _row("alice", "fatima", minutes=0),
        _row("bob", "fatima", minutes=10),
        _row("alice", "fatima", minutes=1440),
        _row("bob", "fatima", minutes=1450),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=24.0, min_overlap=1)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.co_post_count == 2
    assert p.first_seen_at < p.last_seen_at


def test_multiple_targets_accumulate_shared_targets() -> None:
    rows = [
        _row("alice", "fatima", minutes=0),
        _row("bob", "fatima", minutes=5),
        _row("alice", "antenor", minutes=120),
        _row("bob", "antenor", minutes=125),
        _row("alice", "rogerio", minutes=240),
        _row("bob", "rogerio", minutes=245),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=2)
    assert len(pairs) == 1
    p = pairs[0]
    assert p.co_post_count == 3
    assert p.shared_targets == ("antenor", "fatima", "rogerio")
    assert p.jaccard == 1.0


def test_sort_order_prioritizes_co_post_count() -> None:
    """Output sorted desc by co_post_count, then jaccard."""
    rows = [
        # Pair (alice, bob): 1 co-post, jaccard 1.0
        _row("alice", "fatima", minutes=0),
        _row("bob", "fatima", minutes=5),
        # Pair (carol, dan): 3 co-posts, jaccard ~lower (more solo targets)
        _row("carol", "antenor", minutes=10),
        _row("dan", "antenor", minutes=15),
        _row("carol", "antenor", minutes=120),
        _row("dan", "antenor", minutes=125),
        _row("carol", "antenor", minutes=240),
        _row("dan", "antenor", minutes=245),
        _row("carol", "rogerio", minutes=400),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=1)
    assert pairs[0].co_post_count == 3  # carol/dan first
    assert pairs[1].co_post_count == 1


def test_invalid_published_at_raises_typeerror() -> None:
    rows = [
        {
            "author_id": "alice",
            "platform": "x",
            "person_target": "fatima",
            "published_at": "2026-05-10T12:00:00Z",  # str, not datetime
        }
    ]
    with pytest.raises(TypeError, match="datetime"):
        compute_coactivation_scores(rows)


def test_negative_window_hours_rejected() -> None:
    with pytest.raises(ValueError, match="window_hours"):
        compute_coactivation_scores([], window_hours=0)


def test_min_overlap_zero_rejected() -> None:
    with pytest.raises(ValueError, match="min_overlap"):
        compute_coactivation_scores([], min_overlap=0)


def test_persona_lookup_collapses_cross_platform_handles() -> None:
    """v2b persona_lookup collapses cross-platform same-author into one node."""
    rows = [
        _row("zoey", "fatima", minutes=0, platform="facebook"),
        _row("zoey", "fatima", minutes=5, platform="instagram"),
        _row("alice", "fatima", minutes=10, platform="x"),
    ]
    # Without persona_lookup: 3 distinct authors → all three pair up.
    pairs_v1 = compute_coactivation_scores(rows, min_overlap=1)
    assert len(pairs_v1) == 3

    # With persona_lookup collapsing (facebook, zoey) and (instagram, zoey)
    # to one persona: only 2 distinct nodes remain, so only 1 pair emits.
    lookup = {("facebook", "zoey"): "p_zoey", ("instagram", "zoey"): "p_zoey"}
    pairs_v2 = compute_coactivation_scores(rows, min_overlap=1, persona_lookup=lookup)
    assert len(pairs_v2) == 1
    # Persona-keyed nodes carry the sentinel platform.
    keys = {pairs_v2[0].author_a, pairs_v2[0].author_b}
    assert AuthorKey(platform="persona", author_id="p_zoey") in keys
    assert AuthorKey(platform="x", author_id="alice") in keys


def test_persona_lookup_default_none_preserves_v1_behavior() -> None:
    rows = [
        _row("zoey", "fatima", minutes=0, platform="facebook"),
        _row("zoey", "fatima", minutes=5, platform="instagram"),
    ]
    # No lookup → v1 (platform, author_id) keying — they remain distinct.
    pairs = compute_coactivation_scores(rows, min_overlap=1)
    assert len(pairs) == 1
    assert pairs[0].author_a.platform != pairs[0].author_b.platform


def test_row_with_empty_target_is_skipped() -> None:
    rows = [
        _row("alice", "", minutes=0),
        _row("bob", "", minutes=5),
        _row("alice", "fatima", minutes=10),
        _row("bob", "fatima", minutes=15),
    ]
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=1)
    assert len(pairs) == 1
    assert pairs[0].shared_targets == ("fatima",)


# ─── Eixo 3 v3 — content similarity ─────────────────────────────────────────


def _row_with_hash(
    author: str,
    target: str,
    content_hash: str,
    *,
    minutes: float = 0.0,
    platform: str = "x",
) -> dict:
    r = _row(author, target, minutes=minutes, platform=platform)
    r["content_hash"] = content_hash
    return r


def _coordinated_rows(
    alice_hash: str = "ha1",
    bob_hash: str = "hb1",
    n_windows: int = 3,
) -> list[dict]:
    """n_windows non-overlapping 60-min windows each with alice + bob.

    Each window is spaced 2 hours apart so they don't overlap.
    alice posts at window_start, bob at window_start+10 min.
    co_post_count = n_windows (one anchor per window).
    """
    rows = []
    for i in range(n_windows):
        offset = i * 120  # 2h between windows
        rows.append(_row_with_hash("alice", "fatima", alice_hash, minutes=offset))
        rows.append(_row_with_hash("bob", "fatima", bob_hash, minutes=offset + 10))
    return rows


def test_content_sim_none_when_no_embeddings_supplied() -> None:
    rows = _coordinated_rows()
    pairs = compute_coactivation_scores(rows, window_hours=1.0, min_overlap=3)
    assert len(pairs) == 1
    assert pairs[0].avg_content_similarity is None


def test_content_sim_computed_when_embeddings_supplied() -> None:
    # Embeddings for ha1 and hb1 are identical → cosine sim = 1.0
    emb = [1.0, 0.0, 0.0]
    rows = _coordinated_rows()
    pairs = compute_coactivation_scores(
        rows,
        window_hours=1.0,
        min_overlap=3,
        content_embeddings={"ha1": emb, "hb1": emb},
    )
    assert len(pairs) == 1
    assert pairs[0].avg_content_similarity == pytest.approx(1.0)


def test_content_sim_none_when_author_has_no_embedding_coverage() -> None:
    # Only one author has embeddings → can't compute pairwise sim.
    emb = [1.0, 0.0, 0.0]
    rows = _coordinated_rows()
    # Only alice has an embedding; bob does not.
    pairs = compute_coactivation_scores(
        rows,
        window_hours=1.0,
        min_overlap=3,
        content_embeddings={"ha1": emb},
    )
    assert len(pairs) == 1
    assert pairs[0].avg_content_similarity is None
