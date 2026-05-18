"""Compute author co-activation scores over silver activations — Eixo 3 v1.

Input shape mirrors ``mapear_social.models.SilverAuthorActivation`` but
accepts plain dicts so the module stays decoupled from the social
package (mapear-nlp ↛ mapear-social). Required keys per row:

  * ``author_id``      str — handle / surrogate identity (v1)
  * ``platform``       str — facebook | instagram | x | tiktok
  * ``person_target``  str — the mentioned person
  * ``published_at``   datetime — for window bucketing

Optional: ``post_id`` (deduplication safety), ``target_person_id``
(carried through; not used for grouping in v1).

Algorithm
---------
1. Group activations by ``person_target``.
2. Within a target, sort by ``published_at`` and sweep a sliding window
   of ``window_hours``. For every pair of *distinct* authors with at
   least one activation each inside that window, record one co-post
   event.
3. Aggregate pair-level statistics:
     * ``co_post_count``     — number of windowed co-occurrences across
       all targets and windows (one increment per (a, b, target,
       window-bucket)).
     * ``shared_targets``    — sorted tuple of distinct person_target
       strings the pair both activated on, ever (not window-bounded).
     * ``jaccard``           — |T_a ∩ T_b| / |T_a ∪ T_b| over the
       *lifetime* target sets (all activations in the input, not
       window-bounded). Captures sustained alignment.
     * ``first_seen_at``     — earliest co-post window seen.
     * ``last_seen_at``      — latest co-post window seen.
4. Drop pairs with ``co_post_count < min_overlap``.

Why two count regimes (windowed co-posts vs lifetime Jaccard)
------------------------------------------------------------
Coordinated authors tend to score high on *both*: they fire together
inside a window (synchrony) *and* they fire about the same people over
time (alignment). A pair that scores high on only one is a weak signal
on its own. v3 will combine them into a single inauthenticity score;
v1 emits both raw signals.

Anti-objectives (v1)
--------------------
  * No clustering — pair output, not communities.
  * No cross-platform identity resolution — ``author_id`` is the
    surrogate. (Same handle on two platforms counts as two authors.)
  * No content similarity / LLM judging.
  * No streaming — all activations are processed in a single batch.

See ADR docs/decisions/adr-eixo-3-v1-coactivation-graph.md.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True, order=True)
class AuthorKey:
    """Stable identity for an author within the v1 graph.

    ``(platform, author_id)`` — same handle on FB and IG are two distinct
    authors in v1. Cross-platform identity resolution is the v2
    deliverable; until then this surrogate is honest about not knowing.
    """

    platform: str
    author_id: str


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity ∈ [-1, 1]. Returns 0.0 on zero vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


@dataclass(frozen=True)
class AuthorPair:
    """Co-activation statistics for one unordered pair of authors.

    ``author_a`` and ``author_b`` are sorted (a < b on the tuple
    ordering) so ``(a, b)`` is the canonical pair key — never both
    ``(a, b)`` and ``(b, a)``.
    """

    author_a: AuthorKey
    author_b: AuthorKey
    co_post_count: int
    shared_targets: tuple[str, ...]
    jaccard: float
    first_seen_at: datetime
    last_seen_at: datetime
    # Eixo 3 v3 — average cosine similarity between author post embeddings.
    # None when no content_embeddings are supplied to compute_coactivation_scores.
    avg_content_similarity: float | None = field(default=None, compare=False)


_PERSONA_PLATFORM_SENTINEL = "persona"


PersonaLookup = Mapping[tuple[str, str], str]


def _author_key(
    row: Mapping[str, Any],
    persona_lookup: PersonaLookup | None = None,
) -> AuthorKey:
    """Resolve a row to its graph identity.

    With no ``persona_lookup`` the result is the v1 surrogate
    ``(platform, author_id)``. When a lookup is provided and the row's
    ``(platform, author_id)`` is present, the key collapses to
    ``("persona", persona_id)`` — same handle on FB and IG that share
    a persona become one node. The sentinel platform string keeps the
    output tuple-shape stable so downstream code (community detection,
    dbt staging) does not need a new column to know it's seeing a
    persona-keyed graph; ``platform == "persona"`` is the marker.
    """
    platform = str(row["platform"])
    author_id = str(row["author_id"])
    if persona_lookup is not None:
        persona_id = persona_lookup.get((platform, author_id))
        if persona_id is not None:
            return AuthorKey(platform=_PERSONA_PLATFORM_SENTINEL, author_id=persona_id)
    return AuthorKey(platform=platform, author_id=author_id)


def _windowed_pairs(
    target_rows: list[Mapping[str, Any]],
    window: timedelta,
    persona_lookup: PersonaLookup | None = None,
) -> Iterable[tuple[AuthorKey, AuthorKey, datetime]]:
    """Yield (author_a, author_b, window_anchor) per co-post window.

    ``target_rows`` is assumed pre-sorted by ``published_at``.
    Within each maximal contiguous run where ``last - first <= window``,
    every pair of distinct authors contributes exactly one event,
    anchored at the run's earliest timestamp. A row whose author is the
    same as a neighbour inside the window does not produce a
    self-co-post; only distinct authors pair up.

    This is the simplest window semantics that v1 needs: counts go up
    when two *different* people activate against the same target close
    in time. It does not double-count when an author posts multiple
    times inside the same window.
    """
    if not target_rows:
        return
    n = len(target_rows)
    left = 0
    seen_anchors: set[tuple[AuthorKey, AuthorKey, datetime]] = set()
    for right in range(n):
        while (
            target_rows[right]["published_at"] - target_rows[left]["published_at"]
            > window
        ):
            left += 1
        # Authors present in the [left, right] window.
        window_authors_with_ts: dict[AuthorKey, datetime] = {}
        for i in range(left, right + 1):
            key = _author_key(target_rows[i], persona_lookup)
            ts = target_rows[i]["published_at"]
            # Earliest activation per author inside the window — anchor
            # picks the genuine first co-occurrence.
            prev = window_authors_with_ts.get(key)
            if prev is None or ts < prev:
                window_authors_with_ts[key] = ts
        if len(window_authors_with_ts) < 2:
            continue
        authors_sorted = sorted(window_authors_with_ts)
        for i in range(len(authors_sorted)):
            for j in range(i + 1, len(authors_sorted)):
                a, b = authors_sorted[i], authors_sorted[j]
                anchor = max(window_authors_with_ts[a], window_authors_with_ts[b])
                # Dedup: when the sliding window grows but the same
                # pair-anchor recurs across iterations, count once.
                key = (a, b, anchor)
                if key in seen_anchors:
                    continue
                seen_anchors.add(key)
                yield a, b, anchor


def compute_coactivation_scores(
    activations: Iterable[Mapping[str, Any]],
    *,
    window_hours: float = 24.0,
    min_overlap: int = 3,
    persona_lookup: PersonaLookup | None = None,
    content_embeddings: Mapping[str, Sequence[float]] | None = None,
) -> list[AuthorPair]:
    """Score author pairs over a batch of activations.

    Parameters
    ----------
    activations
        Iterable of dict-like rows. Each row must have ``author_id``,
        ``platform``, ``person_target``, ``published_at``.
    window_hours
        Sliding-window width for co-post detection.
        ``MAPEAR_CIB_WINDOW_HOURS`` is the prod default (24h).
    min_overlap
        Minimum ``co_post_count`` to retain a pair.
        ``MAPEAR_CIB_MIN_OVERLAP`` is the prod default (3).
    persona_lookup
        Eixo 3 v2b — optional ``(platform, author_id) → persona_id``
        mapping. When provided, the engine keys nodes by persona for
        every author that resolved cross-platform; unresolved authors
        still key by ``(platform, author_id)``. v1+v2a output is
        bit-identical when this is ``None`` (default).
    content_embeddings
        Eixo 3 v3 — optional ``content_hash → embedding`` mapping.
        When provided, each emitted ``AuthorPair`` carries
        ``avg_content_similarity``: the mean cosine similarity between
        all post embeddings of ``author_a`` and all of ``author_b``.
        Pairs where either author has no embedding coverage get
        ``avg_content_similarity = None`` even when this dict is
        supplied. v1+v2a output is bit-identical when ``None`` (default).

    Returns
    -------
    list[AuthorPair]
        Sorted by (co_post_count desc, jaccard desc, author_a, author_b).
    """
    if window_hours <= 0:
        raise ValueError("window_hours must be positive")
    if min_overlap < 1:
        raise ValueError("min_overlap must be >= 1")

    window = timedelta(hours=window_hours)

    # Per-author lifetime target sets — feeds Jaccard.
    author_targets: dict[AuthorKey, set[str]] = defaultdict(set)
    # Per-target chronological activations — feeds the windowed sweep.
    target_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # Per-author content hashes — feeds content similarity (v3).
    author_hashes: dict[AuthorKey, set[str]] = defaultdict(set)

    for raw in activations:
        target = raw.get("person_target")
        if not target:
            continue
        published_at = raw.get("published_at")
        if not isinstance(published_at, datetime):
            raise TypeError(
                f"published_at must be datetime, got {type(published_at).__name__}"
            )
        key = _author_key(raw, persona_lookup)
        author_targets[key].add(target)
        target_rows[target].append(dict(raw))
        if content_embeddings is not None:
            h = raw.get("content_hash")
            if h:
                author_hashes[key].add(str(h))

    pair_stats: dict[
        tuple[AuthorKey, AuthorKey],
        dict[str, Any],
    ] = defaultdict(
        lambda: {
            "co_post_count": 0,
            "shared_targets": set(),
            "first_seen_at": None,
            "last_seen_at": None,
        }
    )

    for target, rows in target_rows.items():
        rows.sort(key=lambda r: r["published_at"])
        for a, b, anchor in _windowed_pairs(rows, window, persona_lookup):
            stats = pair_stats[(a, b)]
            stats["co_post_count"] += 1
            stats["shared_targets"].add(target)
            if stats["first_seen_at"] is None or anchor < stats["first_seen_at"]:
                stats["first_seen_at"] = anchor
            if stats["last_seen_at"] is None or anchor > stats["last_seen_at"]:
                stats["last_seen_at"] = anchor

    out: list[AuthorPair] = []
    for (a, b), stats in pair_stats.items():
        if stats["co_post_count"] < min_overlap:
            continue
        t_a = author_targets[a]
        t_b = author_targets[b]
        union = t_a | t_b
        jaccard = (len(t_a & t_b) / len(union)) if union else 0.0

        avg_sim: float | None = None
        if content_embeddings is not None:
            hashes_a = author_hashes.get(a, set())
            hashes_b = author_hashes.get(b, set())
            embeds_a = [
                content_embeddings[h] for h in hashes_a if h in content_embeddings
            ]
            embeds_b = [
                content_embeddings[h] for h in hashes_b if h in content_embeddings
            ]
            if embeds_a and embeds_b:
                sims = [
                    _cosine_similarity(ea, eb) for ea in embeds_a for eb in embeds_b
                ]
                avg_sim = sum(sims) / len(sims)

        out.append(
            AuthorPair(
                author_a=a,
                author_b=b,
                co_post_count=stats["co_post_count"],
                shared_targets=tuple(sorted(stats["shared_targets"])),
                jaccard=jaccard,
                first_seen_at=stats["first_seen_at"],
                last_seen_at=stats["last_seen_at"],
                avg_content_similarity=avg_sim,
            )
        )

    out.sort(key=lambda p: (-p.co_post_count, -p.jaccard, p.author_a, p.author_b))
    return out


__all__ = [
    "AuthorKey",
    "AuthorPair",
    "PersonaLookup",
    "_cosine_similarity",
    "compute_coactivation_scores",
]
