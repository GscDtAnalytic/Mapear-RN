"""Inauthenticity scoring over author pairs and communities — Eixo 3 v3.

Takes the output of ``coactivation.compute_coactivation_scores`` and
``community.detect_communities`` and produces composite scores that
surface the most suspicious clusters for operational review.

Three raw signals per pair
--------------------------
* **Synchrony** — normalised ``co_post_count``:
      synchrony = min(co_post_count / sync_cap, 1.0)
  Fires together often → high score. ``sync_cap`` (default 20) marks
  "saturated coordination"; anything above that still scores 1.0.

* **Alignment** — lifetime Jaccard over target sets.
  Fires about the same people over time → high score. This is
  ``AuthorPair.jaccard`` verbatim.

* **Content similarity** — average cosine similarity between post
  embeddings, from ``AuthorPair.avg_content_similarity``.
  Copies the same text → high score.
  When no embeddings are available this term is omitted and the
  remaining weights are renormalized to 1.0 so the composite remains
  in [0, 1].

Composite score
---------------
  composite = w_sync * synchrony + w_align * alignment [+ w_cs * content_sim]
  (weights renormalized when content_sim is None)

Community-level score
---------------------
``score_communities`` aggregates pair scores within each community.
Only pairs where *both* members belong to the community are counted;
singletons (authors with no in-community edges) score 0.0 on all
dimensions.

See ADR docs/decisions/adr-eixo-3-v3-content-similarity-inauthenticity-scoring.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mapear_nlp.graph.coactivation import AuthorKey, AuthorPair

if TYPE_CHECKING:
    from mapear_nlp.graph.community import CommunityStats


@dataclass(frozen=True)
class ScoringWeights:
    """Weights for the composite inauthenticity score.

    All three weights should sum to 1.0. When ``content_similarity`` is
    non-zero but a pair has no ``avg_content_similarity``, the effective
    weight is redistributed proportionally over the remaining two terms.
    """

    synchrony: float = 0.4
    alignment: float = 0.4
    content_similarity: float = 0.2

    def __post_init__(self) -> None:
        for name, v in (
            ("synchrony", self.synchrony),
            ("alignment", self.alignment),
            ("content_similarity", self.content_similarity),
        ):
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{name} weight must be in [0, 1], got {v}")
        total = self.synchrony + self.alignment + self.content_similarity
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total}")


@dataclass(frozen=True)
class InauthenticityScore:
    """Inauthenticity score for one author pair.

    All component scores are in [0, 1]. ``composite_score`` is the
    weighted combination per ``ScoringWeights``; if ``content_similarity_score``
    is ``None``, the weight is redistributed over synchrony and alignment.
    """

    author_a: AuthorKey
    author_b: AuthorKey
    synchrony_score: float
    alignment_score: float
    content_similarity_score: float | None
    composite_score: float


@dataclass(frozen=True)
class CommunityScore:
    """Aggregate inauthenticity score for one detected community.

    Computed by averaging the ``InauthenticityScore`` of all pairs
    where both endpoints are community members. Communities with zero
    qualifying pairs score 0.0 on all dimensions.
    """

    community_id: int
    member_count: int
    pair_count: int
    avg_synchrony: float
    avg_alignment: float
    avg_content_similarity: float | None
    composite_score: float


def _pair_score(
    pair: AuthorPair,
    weights: ScoringWeights,
    sync_cap: float,
) -> InauthenticityScore:
    synchrony = min(pair.co_post_count / sync_cap, 1.0) if sync_cap > 0 else 0.0
    alignment = pair.jaccard
    cs = pair.avg_content_similarity

    if cs is None:
        w_total = weights.synchrony + weights.alignment
        if w_total == 0.0:
            composite = 0.0
        else:
            composite = (
                weights.synchrony / w_total * synchrony
                + weights.alignment / w_total * alignment
            )
    else:
        composite = (
            weights.synchrony * synchrony
            + weights.alignment * alignment
            + weights.content_similarity * cs
        )

    return InauthenticityScore(
        author_a=pair.author_a,
        author_b=pair.author_b,
        synchrony_score=synchrony,
        alignment_score=alignment,
        content_similarity_score=cs,
        composite_score=composite,
    )


def score_all_pairs(
    pairs: list[AuthorPair],
    weights: ScoringWeights | None = None,
    *,
    sync_cap: float = 20.0,
) -> list[InauthenticityScore]:
    """Compute inauthenticity scores for every pair.

    Parameters
    ----------
    pairs
        Output of ``compute_coactivation_scores``.
    weights
        Score weights. ``None`` uses the default ``ScoringWeights()``.
    sync_cap
        ``co_post_count`` value that maps to synchrony = 1.0.
        Higher values make synchrony harder to saturate.
        ``MAPEAR_CIB_SCORE_SYNC_CAP`` env var (default 20.0).

    Returns
    -------
    list[InauthenticityScore]
        Same length as ``pairs``, sorted by composite_score desc.
    """
    if sync_cap <= 0:
        raise ValueError("sync_cap must be positive")
    if weights is None:
        weights = ScoringWeights()

    out = [_pair_score(p, weights, sync_cap) for p in pairs]
    out.sort(key=lambda s: -s.composite_score)
    return out


def score_communities(
    communities: list[CommunityStats],
    pairs: list[AuthorPair],
    weights: ScoringWeights | None = None,
    *,
    sync_cap: float = 20.0,
) -> list[CommunityScore]:
    """Compute aggregate inauthenticity scores for each community.

    For each community, only pairs where *both* authors appear in
    ``community.members`` are aggregated. Communities with no qualifying
    pairs score 0.0 on all dimensions (this happens when a community
    is a singleton or when its pairs fell below ``min_overlap``).

    Parameters
    ----------
    communities
        Output of ``detect_communities``.
    pairs
        Output of ``compute_coactivation_scores`` — must be the *same*
        activations batch that produced the communities.
    weights
        Score weights. ``None`` uses the default ``ScoringWeights()``.
    sync_cap
        ``MAPEAR_CIB_SCORE_SYNC_CAP`` env var (default 20.0).

    Returns
    -------
    list[CommunityScore]
        Same length as ``communities``, sorted by composite_score desc.
    """
    if sync_cap <= 0:
        raise ValueError("sync_cap must be positive")
    if weights is None:
        weights = ScoringWeights()

    # Index pair scores for quick lookup.
    pair_scores = {
        (s.author_a, s.author_b): s
        for s in score_all_pairs(pairs, weights, sync_cap=sync_cap)
    }

    out: list[CommunityScore] = []
    for community in communities:
        member_set = frozenset(community.members)
        qualifying: list[InauthenticityScore] = []
        for (a, b), score in pair_scores.items():
            if a in member_set and b in member_set:
                qualifying.append(score)

        if not qualifying:
            out.append(
                CommunityScore(
                    community_id=community.community_id,
                    member_count=len(community.members),
                    pair_count=0,
                    avg_synchrony=0.0,
                    avg_alignment=0.0,
                    avg_content_similarity=None,
                    composite_score=0.0,
                )
            )
            continue

        n = len(qualifying)
        avg_sync = sum(s.synchrony_score for s in qualifying) / n
        avg_align = sum(s.alignment_score for s in qualifying) / n
        cs_scores = [
            s.content_similarity_score
            for s in qualifying
            if s.content_similarity_score is not None
        ]
        avg_cs: float | None = sum(cs_scores) / len(cs_scores) if cs_scores else None
        avg_composite = sum(s.composite_score for s in qualifying) / n

        out.append(
            CommunityScore(
                community_id=community.community_id,
                member_count=len(community.members),
                pair_count=n,
                avg_synchrony=avg_sync,
                avg_alignment=avg_align,
                avg_content_similarity=avg_cs,
                composite_score=avg_composite,
            )
        )

    out.sort(key=lambda c: -c.composite_score)
    return out


__all__ = [
    "CommunityScore",
    "InauthenticityScore",
    "ScoringWeights",
    "score_all_pairs",
    "score_communities",
]
