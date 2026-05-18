"""Cross-day cluster-identity persistence — Eixo 3 v3.

Louvain community IDs are assigned fresh each day: adding or removing
a single edge can renumber every cluster. This module solves the identity
problem by:

1. Treating each community as a *set of AuthorKeys* (its ``members``).
2. Computing Jaccard overlap between same-region communities across
   adjacent days.
3. Greedy-matching communities that exceed a Jaccard threshold as
   "the same squad" — they belong to the same **cluster series**.
4. Assigning a stable ``series_id`` (SHA1 over the initial membership)
   that persists as long as the series continues. A new day that
   introduces a completely fresh cluster starts a new series.

Algorithm
---------
For each pair of adjacent dates ``(yesterday, today)`` within a region:
  * Score every (yesterday_community, today_community) pair by Jaccard.
  * Greedily match highest-scoring pairs (greedy bipartite) until no
    remaining pair exceeds ``threshold``.
  * Matched communities inherit the ``series_id`` from yesterday.
  * Unmatched communities start a new series with a fresh ``series_id``.

Series ID stability
-------------------
``series_id = sha1(json(sorted_members_on_first_day))[:16]``.
This is content-addressed: if the same squad resurfaces with identical
members after a gap, it gets the same ID. If membership changes enough
to break the Jaccard threshold, the old series ends and a new one
begins — the split is visible in the data.

Limitations (v3)
----------------
* Greedy matching is O(n²) per day. For realistic CIB graphs (<1000
  communities per day per region) this is < 1 ms.
* One-step lookahead only: if a series disappears for a day and
  reappears the next, it starts a new series. Multi-day gap tracking
  is deferred to a future version.
* Cross-region identity is not attempted — each region is processed
  independently.

See ADR docs/decisions/adr-eixo-3-v3-content-similarity-inauthenticity-scoring.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mapear_nlp.graph.coactivation import AuthorKey
    from mapear_nlp.graph.community import CommunityStats


def _series_id(members: frozenset[AuthorKey]) -> str:
    """Stable content-addressed series id from initial membership."""
    canonical = sorted(f"{m.platform}:{m.author_id}" for m in members)
    raw = hashlib.sha1(json.dumps(canonical).encode()).hexdigest()
    return raw[:16]


def _jaccard_members(
    a: frozenset[AuthorKey],
    b: frozenset[AuthorKey],
) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


@dataclass(frozen=True)
class SeriesAssignment:
    """Cross-day series identity for one community on one date."""

    activation_date: date
    community_id: int
    series_id: str
    series_start_date: date
    jaccard_to_previous: float | None
    is_new_series: bool


def match_communities(
    today: list[CommunityStats],
    yesterday: list[CommunityStats],
    threshold: float = 0.5,
) -> dict[int, int | None]:
    """Greedy bipartite match of today's communities to yesterday's.

    Parameters
    ----------
    today
        Communities detected for the current date.
    yesterday
        Communities detected for the previous date.
    threshold
        Minimum Jaccard similarity to consider two communities the same
        series. ``MAPEAR_CIB_CLUSTER_SERIES_THRESHOLD`` env default 0.5.

    Returns
    -------
    dict[int, int | None]
        ``today_community_id → yesterday_community_id | None``.
        ``None`` means the community has no match and starts a new series.
    """
    if not today or not yesterday:
        return {c.community_id: None for c in today}

    today_sets = {c.community_id: frozenset(c.members) for c in today}
    yesterday_sets = {c.community_id: frozenset(c.members) for c in yesterday}

    # Build scored candidate pairs.
    scored: list[tuple[float, int, int]] = []
    for tid, t_members in today_sets.items():
        for yid, y_members in yesterday_sets.items():
            j = _jaccard_members(t_members, y_members)
            if j >= threshold:
                scored.append((j, tid, yid))

    scored.sort(key=lambda x: -x[0])  # highest Jaccard first

    matched_today: set[int] = set()
    matched_yesterday: set[int] = set()
    assignment: dict[int, int | None] = {}

    for _j, tid, yid in scored:
        if tid in matched_today or yid in matched_yesterday:
            continue
        assignment[tid] = yid
        matched_today.add(tid)
        matched_yesterday.add(yid)

    for c in today:
        if c.community_id not in assignment:
            assignment[c.community_id] = None

    return assignment


def track_cluster_series(
    communities_by_date: dict[date, list[CommunityStats]],
    threshold: float = 0.5,
) -> list[SeriesAssignment]:
    """Assign stable series IDs to all communities across all dates.

    Parameters
    ----------
    communities_by_date
        Map of ``date → list[CommunityStats]`` sorted by date within
        each region group. Typically one region per call (the job
        groups by region before calling this).
    threshold
        Passed to ``match_communities`` for each adjacent pair of dates.

    Returns
    -------
    list[SeriesAssignment]
        One entry per (date, community_id). Sorted by (date, community_id).
    """
    if not communities_by_date:
        return []

    sorted_dates = sorted(communities_by_date.keys())

    # series_id + series_start_date for active series, keyed by yesterday_community_id.
    # On each new day this is rebuilt from the matched assignments.
    series_registry: dict[int, tuple[str, date]] = {}  # yid → (series_id, start_date)

    results: list[SeriesAssignment] = []

    for i, today_date in enumerate(sorted_dates):
        today_communities = communities_by_date[today_date]
        today_sets = {c.community_id: frozenset(c.members) for c in today_communities}

        if i == 0:
            # Bootstrap: every community starts a new series.
            new_registry: dict[int, tuple[str, date]] = {}
            for c in today_communities:
                sid = _series_id(frozenset(c.members))
                new_registry[c.community_id] = (sid, today_date)
                results.append(
                    SeriesAssignment(
                        activation_date=today_date,
                        community_id=c.community_id,
                        series_id=sid,
                        series_start_date=today_date,
                        jaccard_to_previous=None,
                        is_new_series=True,
                    )
                )
            series_registry = new_registry
            continue

        yesterday_date = sorted_dates[i - 1]
        yesterday_communities = communities_by_date[yesterday_date]

        assignment = match_communities(
            today_communities, yesterday_communities, threshold
        )

        # Compute Jaccard scores for matched pairs (for lineage).
        yesterday_sets = {
            c.community_id: frozenset(c.members) for c in yesterday_communities
        }

        new_registry = {}
        for c in today_communities:
            tid = c.community_id
            yid = assignment.get(tid)
            today_members = today_sets[tid]

            if yid is not None and yid in series_registry:
                sid, start_date = series_registry[yid]
                j = _jaccard_members(today_members, yesterday_sets[yid])
                is_new = False
            else:
                sid = _series_id(today_members)
                start_date = today_date
                is_new = True
                results.append(
                    SeriesAssignment(
                        activation_date=today_date,
                        community_id=tid,
                        series_id=sid,
                        series_start_date=start_date,
                        jaccard_to_previous=None,
                        is_new_series=True,
                    )
                )
                new_registry[tid] = (sid, start_date)
                continue

            results.append(
                SeriesAssignment(
                    activation_date=today_date,
                    community_id=tid,
                    series_id=sid,
                    series_start_date=start_date,
                    jaccard_to_previous=j,
                    is_new_series=is_new,
                )
            )
            new_registry[tid] = (sid, start_date)

        series_registry = new_registry

    results.sort(key=lambda r: (r.activation_date, r.community_id))
    return results


__all__ = [
    "SeriesAssignment",
    "match_communities",
    "track_cluster_series",
]
