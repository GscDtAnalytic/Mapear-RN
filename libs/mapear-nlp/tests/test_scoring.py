"""Unit tests for the inauthenticity scoring engine — Eixo 3 v3."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mapear_nlp.graph.coactivation import AuthorKey, AuthorPair
from mapear_nlp.graph.community import CommunityStats
from mapear_nlp.graph.scoring import (
    ScoringWeights,
    score_all_pairs,
    score_communities,
)

_T0 = datetime(2026, 5, 10, tzinfo=UTC)


def _pair(
    a: str = "alice",
    b: str = "bob",
    co_post_count: int = 5,
    jaccard: float = 0.8,
    content_sim: float | None = None,
    platform: str = "x",
) -> AuthorPair:
    return AuthorPair(
        author_a=AuthorKey(platform, a),
        author_b=AuthorKey(platform, b),
        co_post_count=co_post_count,
        shared_targets=("fatima",),
        jaccard=jaccard,
        first_seen_at=_T0,
        last_seen_at=_T0 + timedelta(hours=1),
        avg_content_similarity=content_sim,
    )


def _community(
    cid: int,
    members: list[tuple[str, str]],
    avg_co_post: float = 5.0,
    avg_jaccard: float = 0.8,
) -> CommunityStats:
    return CommunityStats(
        community_id=cid,
        members=tuple(AuthorKey(p, a) for p, a in members),
        edge_count=len(members) - 1,
        edge_density=0.5,
        avg_co_post_count=avg_co_post,
        avg_jaccard=avg_jaccard,
        algorithm="louvain",
    )


# ─── ScoringWeights ───────────────────────────────────────────────────────────


class TestScoringWeights:
    def test_default_weights_sum_to_one(self) -> None:
        w = ScoringWeights()
        assert abs(w.synchrony + w.alignment + w.content_similarity - 1.0) < 1e-9

    def test_custom_valid_weights(self) -> None:
        w = ScoringWeights(synchrony=0.5, alignment=0.3, content_similarity=0.2)
        assert w.synchrony == 0.5

    def test_weights_not_summing_to_one_raises(self) -> None:
        with pytest.raises(ValueError, match="sum to 1.0"):
            ScoringWeights(synchrony=0.5, alignment=0.5, content_similarity=0.2)

    def test_negative_weight_raises(self) -> None:
        with pytest.raises(ValueError):
            ScoringWeights(synchrony=-0.1, alignment=0.6, content_similarity=0.5)


# ─── score_all_pairs ──────────────────────────────────────────────────────────


class TestScoreAllPairs:
    def test_empty_input(self) -> None:
        assert score_all_pairs([]) == []

    def test_synchrony_capped_at_one(self) -> None:
        p = _pair(co_post_count=100)
        scores = score_all_pairs([p], sync_cap=20.0)
        assert scores[0].synchrony_score == 1.0

    def test_synchrony_partial(self) -> None:
        p = _pair(co_post_count=10)
        scores = score_all_pairs([p], sync_cap=20.0)
        assert abs(scores[0].synchrony_score - 0.5) < 1e-9

    def test_alignment_equals_jaccard(self) -> None:
        p = _pair(jaccard=0.6)
        scores = score_all_pairs([p])
        assert scores[0].alignment_score == 0.6

    def test_content_sim_none_when_pair_has_none(self) -> None:
        p = _pair(content_sim=None)
        scores = score_all_pairs([p])
        assert scores[0].content_similarity_score is None

    def test_content_sim_propagated(self) -> None:
        p = _pair(content_sim=0.9)
        scores = score_all_pairs([p])
        assert scores[0].content_similarity_score == pytest.approx(0.9)

    def test_composite_no_content_sim_redistributes_weights(self) -> None:
        w = ScoringWeights()  # 0.4 sync, 0.4 align, 0.2 cs
        p = _pair(co_post_count=20, jaccard=1.0, content_sim=None)
        scores = score_all_pairs([p], weights=w, sync_cap=20.0)
        # weights redistributed: sync/(sync+align) = 0.5, align/(sync+align) = 0.5
        # composite = 0.5 * 1.0 + 0.5 * 1.0 = 1.0
        assert scores[0].composite_score == pytest.approx(1.0)

    def test_composite_with_content_sim(self) -> None:
        w = ScoringWeights(synchrony=0.4, alignment=0.4, content_similarity=0.2)
        p = _pair(co_post_count=20, jaccard=1.0, content_sim=1.0)
        scores = score_all_pairs([p], weights=w, sync_cap=20.0)
        assert scores[0].composite_score == pytest.approx(1.0)

    def test_composite_mixed_signals(self) -> None:
        w = ScoringWeights(synchrony=0.4, alignment=0.4, content_similarity=0.2)
        # sync=0.5 (10/20), align=0.5, cs=0.5
        p = _pair(co_post_count=10, jaccard=0.5, content_sim=0.5)
        scores = score_all_pairs([p], weights=w, sync_cap=20.0)
        expected = 0.4 * 0.5 + 0.4 * 0.5 + 0.2 * 0.5
        assert scores[0].composite_score == pytest.approx(expected)

    def test_sorted_by_composite_desc(self) -> None:
        pairs = [
            _pair("a", "b", co_post_count=5, jaccard=0.3),
            _pair("c", "d", co_post_count=20, jaccard=0.9),
        ]
        scores = score_all_pairs(pairs)
        assert scores[0].composite_score >= scores[1].composite_score

    def test_sync_cap_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            score_all_pairs([_pair()], sync_cap=0.0)

    def test_author_keys_propagated(self) -> None:
        p = _pair("alice", "bob")
        scores = score_all_pairs([p])
        assert scores[0].author_a == AuthorKey("x", "alice")
        assert scores[0].author_b == AuthorKey("x", "bob")


# ─── score_communities ────────────────────────────────────────────────────────


class TestScoreCommunities:
    def test_empty_input(self) -> None:
        assert score_communities([], []) == []

    def test_community_with_no_qualifying_pairs_scores_zero(self) -> None:
        community = _community(0, [("x", "alice"), ("x", "bob")])
        # No pairs at all.
        scores = score_communities([community], [])
        assert len(scores) == 1
        assert scores[0].composite_score == 0.0
        assert scores[0].pair_count == 0

    def test_community_score_aggregates_pairs(self) -> None:
        community = _community(0, [("x", "alice"), ("x", "bob")])
        p = _pair("alice", "bob", co_post_count=20, jaccard=1.0)
        scores = score_communities([community], [p], sync_cap=20.0)
        assert scores[0].avg_synchrony == pytest.approx(1.0)
        assert scores[0].avg_alignment == pytest.approx(1.0)
        assert scores[0].composite_score == pytest.approx(1.0)

    def test_pairs_outside_community_ignored(self) -> None:
        community = _community(0, [("x", "alice"), ("x", "bob")])
        in_pair = _pair("alice", "bob", co_post_count=20, jaccard=1.0)
        out_pair = _pair("carol", "dave", co_post_count=20, jaccard=1.0)
        scores = score_communities([community], [in_pair, out_pair], sync_cap=20.0)
        assert scores[0].pair_count == 1

    def test_content_sim_none_when_no_embeddings(self) -> None:
        community = _community(0, [("x", "alice"), ("x", "bob")])
        p = _pair("alice", "bob", content_sim=None)
        scores = score_communities([community], [p])
        assert scores[0].avg_content_similarity is None

    def test_sorted_by_composite_desc(self) -> None:
        c1 = _community(0, [("x", "alice"), ("x", "bob")])
        c2 = _community(1, [("x", "carol"), ("x", "dave")])
        p1 = _pair("alice", "bob", co_post_count=20, jaccard=0.9)
        p2 = _pair("carol", "dave", co_post_count=2, jaccard=0.1)
        scores = score_communities([c1, c2], [p1, p2], sync_cap=20.0)
        assert scores[0].composite_score >= scores[1].composite_score

    def test_community_size_and_member_count_match(self) -> None:
        community = _community(0, [("x", "alice"), ("x", "bob"), ("x", "carol")])
        p1 = _pair("alice", "bob")
        p2 = _pair("alice", "carol")
        scores = score_communities([community], [p1, p2])
        assert scores[0].member_count == 3
