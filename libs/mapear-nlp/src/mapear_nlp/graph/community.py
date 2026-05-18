"""Community detection over the author co-activation graph — Eixo 3 v2a.

Takes the pair output of ``mapear_nlp.graph.coactivation`` and lifts it
from "who fires with whom" (pairs) to "who belongs together" (clusters).
Communities are the natural unit at which CIB campaigns operate —
a coordinated squad of 10 accounts produces O(45) noisy pair signals
that collapse to one cluster.

v2a deliberately stays scoped to the algorithms networkx ships:
**Louvain** (default, modularity-optimising) and **label propagation**
(deterministic, fast, fuzzier). Leiden — better than Louvain on weakly
connected components — would add the ``leidenalg`` C-extension; deferred
to a future iteration once we see a real graph that punishes Louvain.

Inputs
------
The graph is built from ``AuthorPair`` rows emitted by
``mapear_nlp.graph.coactivation.compute_coactivation_scores``. Edge
weight is ``co_post_count`` (synchrony signal); ``jaccard`` and
``shared_targets`` are stored as edge attributes for downstream
filtering. Since AuthorPair enforces ``author_a < author_b``, the
graph is naturally undirected and self-loop-free.

Outputs
-------
``detect_communities`` returns ``CommunityStats`` rows — one per
emitted cluster — sorted by member count desc. Communities with fewer
than ``min_size`` members are dropped: a 2-node clique is a pair, not
a community, and surfaces in the v1 mart already.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import networkx as nx
from networkx.algorithms.community import (
    label_propagation_communities,
    louvain_communities,
)

from mapear_nlp.graph.coactivation import AuthorKey, AuthorPair

Algorithm = Literal["louvain", "label_propagation"]


@dataclass(frozen=True)
class CommunityStats:
    """Aggregate statistics for one detected community.

    Members are sorted (AuthorKey ordering) so the tuple is the canonical
    membership identity — two runs that produce the same cluster yield
    the same ``members`` tuple regardless of iteration order.
    """

    community_id: int
    members: tuple[AuthorKey, ...]
    edge_count: int
    edge_density: float
    avg_co_post_count: float
    avg_jaccard: float
    algorithm: Algorithm


def build_graph(pairs: Iterable[AuthorPair]) -> nx.Graph:
    """Construct a weighted undirected graph from author pairs.

    Edge attributes:
      * ``weight``         — co_post_count (synchrony signal; primary
        weight for modularity-based algorithms).
      * ``jaccard``        — lifetime target-set Jaccard.
      * ``shared_targets`` — tuple of shared person_target strings.

    Isolated authors (zero pair appearances) are NOT added — communities
    are about who fires together; a node with no co-fires is not a
    community member by construction.
    """
    g: nx.Graph = nx.Graph()
    for p in pairs:
        if p.author_a == p.author_b:
            # Defensive: AuthorPair guarantees a < b, so this branch is
            # unreachable. Keep the check so future refactors don't
            # silently introduce self-loops.
            continue
        g.add_edge(
            p.author_a,
            p.author_b,
            weight=p.co_post_count,
            jaccard=p.jaccard,
            shared_targets=p.shared_targets,
        )
    return g


def detect_communities(
    graph: nx.Graph,
    *,
    algorithm: Algorithm = "louvain",
    resolution: float = 1.0,
    seed: int | None = 42,
    min_size: int = 3,
) -> list[CommunityStats]:
    """Find communities in the author pair graph.

    Parameters
    ----------
    graph
        Output of ``build_graph``. Empty graph → empty list.
    algorithm
        ``"louvain"`` (default) maximises modularity, recommended for
        tight squads. ``"label_propagation"`` is deterministic and
        faster on large graphs but produces fuzzier boundaries.
    resolution
        Louvain resolution. ``1.0`` is the modularity standard; ``>1``
        biases toward smaller / tighter clusters. Ignored for
        label_propagation.
    seed
        Random seed for Louvain. Set ``None`` only if running multiple
        seeds and aggregating; the v2a job uses the configured
        ``MAPEAR_CIB_COMMUNITY_SEED`` (default 42) for stability.
    min_size
        Drop communities with fewer than ``min_size`` members. v2a
        default ``MAPEAR_CIB_COMMUNITY_MIN_SIZE=3``; the smallest unit
        of "coordination" we want to surface as a cluster.

    Returns
    -------
    list[CommunityStats]
        Sorted by (member_count desc, community_id asc).
    """
    if min_size < 2:
        raise ValueError("min_size must be >= 2")
    if graph.number_of_nodes() == 0:
        return []

    if algorithm == "louvain":
        raw_communities = louvain_communities(
            graph, weight="weight", resolution=resolution, seed=seed
        )
    elif algorithm == "label_propagation":
        raw_communities = list(label_propagation_communities(graph))
    else:  # pragma: no cover — Literal enforces at type-check time
        raise ValueError(f"unknown algorithm: {algorithm}")

    # Sort communities deterministically by member tuple before
    # assigning IDs — same input, same IDs across runs, regardless of
    # the algorithm's internal iteration order.
    sorted_communities = sorted(
        (tuple(sorted(c)) for c in raw_communities),
        key=lambda members: (-len(members), members),
    )

    out: list[CommunityStats] = []
    for cid, members in enumerate(sorted_communities):
        if len(members) < min_size:
            continue
        sub = graph.subgraph(members)
        edge_count = sub.number_of_edges()
        density = nx.density(sub) if sub.number_of_nodes() > 1 else 0.0
        if edge_count:
            avg_weight = (
                sum(d["weight"] for _, _, d in sub.edges(data=True)) / edge_count
            )
            avg_jaccard = (
                sum(d["jaccard"] for _, _, d in sub.edges(data=True)) / edge_count
            )
        else:
            avg_weight = 0.0
            avg_jaccard = 0.0
        out.append(
            CommunityStats(
                community_id=cid,
                members=members,
                edge_count=edge_count,
                edge_density=density,
                avg_co_post_count=avg_weight,
                avg_jaccard=avg_jaccard,
                algorithm=algorithm,
            )
        )
    return out


__all__ = ["Algorithm", "CommunityStats", "build_graph", "detect_communities"]
