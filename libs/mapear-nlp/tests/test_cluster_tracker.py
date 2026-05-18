"""Unit tests for cross-day cluster-identity persistence — Eixo 3 v3."""

from __future__ import annotations

from datetime import UTC, date

from mapear_nlp.graph.cluster_tracker import (
    match_communities,
    track_cluster_series,
)
from mapear_nlp.graph.coactivation import AuthorKey
from mapear_nlp.graph.community import CommunityStats

UTC = UTC


def _mk(platform: str, author: str) -> AuthorKey:
    return AuthorKey(platform, author)


def _community(cid: int, members: list[str], platform: str = "x") -> CommunityStats:
    return CommunityStats(
        community_id=cid,
        members=tuple(_mk(platform, m) for m in members),
        edge_count=len(members) - 1,
        edge_density=0.5,
        avg_co_post_count=5.0,
        avg_jaccard=0.8,
        algorithm="louvain",
    )


_D0 = date(2026, 5, 10)
_D1 = date(2026, 5, 11)
_D2 = date(2026, 5, 12)


# ─── match_communities ────────────────────────────────────────────────────────


class TestMatchCommunities:
    def test_empty_today(self) -> None:
        yesterday = [_community(0, ["alice", "bob"])]
        result = match_communities([], yesterday)
        assert result == {}

    def test_empty_yesterday(self) -> None:
        today = [_community(0, ["alice", "bob"])]
        result = match_communities(today, [])
        assert result == {0: None}

    def test_identical_communities_match(self) -> None:
        today = [_community(0, ["alice", "bob", "carol"])]
        yesterday = [_community(99, ["alice", "bob", "carol"])]
        result = match_communities(today, yesterday, threshold=0.5)
        assert result[0] == 99

    def test_overlapping_communities_match(self) -> None:
        # Jaccard = |{alice, bob}| / |{alice, bob, carol}| = 2/3 ≈ 0.67 > 0.5
        today = [_community(0, ["alice", "bob", "carol"])]
        yesterday = [_community(5, ["alice", "bob"])]
        result = match_communities(today, yesterday, threshold=0.5)
        assert result[0] == 5

    def test_below_threshold_not_matched(self) -> None:
        # Jaccard = 1/4 = 0.25 < 0.5
        today = [_community(0, ["alice", "bob", "carol", "dave"])]
        yesterday = [_community(5, ["alice"])]
        result = match_communities(today, yesterday, threshold=0.5)
        assert result[0] is None

    def test_greedy_best_match_first(self) -> None:
        # Two today, two yesterday; best match takes precedence.
        today = [
            _community(0, ["alice", "bob"]),
            _community(1, ["carol", "dave"]),
        ]
        yesterday = [
            _community(10, ["alice", "bob"]),  # perfect match for 0
            _community(11, ["carol", "dave"]),  # perfect match for 1
        ]
        result = match_communities(today, yesterday, threshold=0.5)
        assert result[0] == 10
        assert result[1] == 11

    def test_no_double_matching(self) -> None:
        # today[0] and today[1] both overlap with yesterday[0]; only one wins.
        today = [
            _community(0, ["alice", "bob"]),
            _community(1, ["alice", "carol"]),
        ]
        yesterday = [_community(99, ["alice", "bob"])]
        result = match_communities(today, yesterday, threshold=0.5)
        matched = [cid for cid, yid in result.items() if yid == 99]
        assert len(matched) == 1

    def test_completely_disjoint_no_match(self) -> None:
        today = [_community(0, ["alice", "bob"])]
        yesterday = [_community(99, ["carol", "dave"])]
        result = match_communities(today, yesterday, threshold=0.5)
        assert result[0] is None


# ─── track_cluster_series ─────────────────────────────────────────────────────


class TestTrackClusterSeries:
    def test_empty_input(self) -> None:
        assert track_cluster_series({}) == []

    def test_single_day_all_new(self) -> None:
        communities = {_D0: [_community(0, ["alice", "bob"])]}
        results = track_cluster_series(communities)
        assert len(results) == 1
        r = results[0]
        assert r.is_new_series is True
        assert r.jaccard_to_previous is None
        assert r.series_start_date == _D0

    def test_series_id_stable_across_identical_membership(self) -> None:
        c0 = _community(0, ["alice", "bob"])
        c1 = _community(0, ["alice", "bob"])  # same members, new day
        communities = {_D0: [c0], _D1: [c1]}
        results = track_cluster_series(communities, threshold=0.5)
        assert len(results) == 2
        r0 = next(r for r in results if r.activation_date == _D0)
        r1 = next(r for r in results if r.activation_date == _D1)
        assert r0.series_id == r1.series_id

    def test_series_continues_with_member_overlap(self) -> None:
        today = [_community(0, ["alice", "bob", "carol"])]
        tomorrow = [_community(0, ["alice", "bob", "dave"])]
        # Jaccard = 2/4 = 0.5 ≥ threshold
        communities = {_D0: today, _D1: tomorrow}
        results = track_cluster_series(communities, threshold=0.5)
        r0 = next(r for r in results if r.activation_date == _D0)
        r1 = next(r for r in results if r.activation_date == _D1)
        assert r1.series_id == r0.series_id
        assert r1.is_new_series is False
        assert r1.jaccard_to_previous is not None

    def test_new_series_when_disjoint(self) -> None:
        today = [_community(0, ["alice", "bob"])]
        tomorrow = [_community(0, ["carol", "dave"])]
        communities = {_D0: today, _D1: tomorrow}
        results = track_cluster_series(communities, threshold=0.5)
        r0 = next(r for r in results if r.activation_date == _D0)
        r1 = next(r for r in results if r.activation_date == _D1)
        assert r1.series_id != r0.series_id
        assert r1.is_new_series is True

    def test_multiple_communities_tracked_independently(self) -> None:
        c_a_d0 = _community(0, ["alice", "bob"])
        c_b_d0 = _community(1, ["carol", "dave"])
        c_a_d1 = _community(0, ["alice", "bob"])
        c_b_d1 = _community(1, ["carol", "dave"])
        communities = {_D0: [c_a_d0, c_b_d0], _D1: [c_a_d1, c_b_d1]}
        results = track_cluster_series(communities, threshold=0.5)
        d0_map = {r.community_id: r for r in results if r.activation_date == _D0}
        d1_map = {r.community_id: r for r in results if r.activation_date == _D1}
        assert d1_map[0].series_id == d0_map[0].series_id
        assert d1_map[1].series_id == d0_map[1].series_id
        assert d0_map[0].series_id != d0_map[1].series_id

    def test_results_sorted_by_date_and_community(self) -> None:
        communities = {
            _D1: [_community(1, ["alice", "bob"]), _community(0, ["carol"])],
            _D0: [_community(0, ["alice"])],
        }
        results = track_cluster_series(communities)
        dates = [(r.activation_date, r.community_id) for r in results]
        assert dates == sorted(dates)

    def test_series_id_deterministic(self) -> None:
        communities = {_D0: [_community(0, ["alice", "bob"])]}
        r1 = track_cluster_series(communities)
        r2 = track_cluster_series(communities)
        assert r1[0].series_id == r2[0].series_id

    def test_three_day_series_persists(self) -> None:
        same_members = ["alice", "bob", "carol"]
        communities = {
            _D0: [_community(0, same_members)],
            _D1: [_community(0, same_members)],
            _D2: [_community(0, same_members)],
        }
        results = track_cluster_series(communities, threshold=0.5)
        sids = {r.series_id for r in results}
        assert len(sids) == 1  # all same series
