"""Author co-activation graph — Eixo 3 v1 (CIB detection foundation).

The ``coactivation`` module ingests rows shaped like
``SilverAuthorActivation`` and emits scored author pairs that activated
against the same ``person_target`` within a configurable time window.
v1 is deliberately pure-Python (no networkx, no clustering) — see
docs/decisions/adr-eixo-3-v1-coactivation-graph.md for the scope rules.
"""

from mapear_nlp.graph.coactivation import (
    AuthorKey,
    AuthorPair,
    compute_coactivation_scores,
)

__all__ = [
    "AuthorKey",
    "AuthorPair",
    "compute_coactivation_scores",
]

# Eixo 3 v2a — community detection. Imported lazily to keep mapear-nlp
# import-time light when the graph extra is not installed.
try:
    from mapear_nlp.graph.community import (
        Algorithm,
        CommunityStats,
        build_graph,
        detect_communities,
    )

    __all__ += ["Algorithm", "CommunityStats", "build_graph", "detect_communities"]
except ImportError:  # networkx not installed — v1 engine still usable.
    pass
