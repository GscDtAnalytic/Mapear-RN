"""Narrative clustering over sentence embeddings — Eixo 2 v2a.

Takes ``(content_hash, embedding)`` pairs produced by the embedding
pipeline and groups narratives into clusters. Clusters are the natural
unit at which coordinated framing operates — twenty articles repeating
"governo destruiu hospitais" land in one cluster instead of twenty rows.

v2a ships two algorithms:

* **HDBSCAN** (default): density-based, no fixed k, marks outliers
  explicitly as ``cluster_id = -1``. The right tool when you do not
  know how many narrative threads are active on a given day. Requires
  the optional ``embeddings`` dep group (hdbscan).
* **cosine_threshold**: pure-Python connected-components on a graph
  where edges link narratives with cosine similarity ≥ threshold. No
  new dep — preserved so the eval gate and CI can run when hdbscan
  is not installed. Less expressive (no soft outliers) but transparent.

Cluster IDs are deterministically assigned by sorting clusters by
``(member_count desc, sorted-membership asc)`` so identical inputs
produce identical IDs across runs and across algorithms.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Algorithm = Literal["hdbscan", "cosine_threshold"]


@dataclass(frozen=True)
class NarrativeClusterAssignment:
    """One narrative's cluster assignment, post-detection.

    ``cluster_id = -1`` marks an outlier (HDBSCAN noise label). Outliers
    still receive a row in the output so the operator can see which
    narratives were too lonely to join a cluster.
    """

    content_hash: str
    cluster_id: int
    member_role: Literal["centroid", "member", "outlier"]
    distance_to_centroid: float | None


@dataclass(frozen=True)
class NarrativeCluster:
    """Aggregate statistics for one detected cluster.

    Members are sorted by content_hash so the tuple is the canonical
    membership identity — two runs that produce the same cluster yield
    the same ``members`` tuple regardless of detector iteration order.
    """

    cluster_id: int
    members: tuple[str, ...]  # content_hashes, sorted
    centroid: tuple[float, ...]  # mean of member embeddings
    cluster_size: int
    avg_intra_cluster_distance: float
    algorithm: Algorithm


@dataclass(frozen=True)
class ClusteringResult:
    """Output of one clustering run.

    ``assignments`` covers every input narrative (including outliers).
    ``clusters`` covers only non-outlier clusters that pass min_size.
    """

    assignments: list[NarrativeClusterAssignment]
    clusters: list[NarrativeCluster]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance on (possibly unnormalised) vectors.

    Returns ``1 - cosine_similarity`` clamped to [0, 2]. Used for both
    intra-cluster distance and centroid distance. We re-normalise here
    rather than trusting upstream because the cosine_threshold algorithm
    accepts any embedding source.
    """
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 1.0
    sim = dot / (math.sqrt(na) * math.sqrt(nb))
    # Clamp for numerical safety; mpnet outputs are unit-norm but the
    # pure-Python recomputation can drift by ~1e-9 over 768 dims.
    sim = max(-1.0, min(1.0, sim))
    return 1.0 - sim


def _centroid(vectors: list[list[float]]) -> list[float]:
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            acc[i] += x
    n = float(len(vectors))
    return [x / n for x in acc]


def _avg_pairwise_distance(vectors: list[list[float]]) -> float:
    n = len(vectors)
    if n < 2:
        return 0.0
    total = 0.0
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += _cosine_distance(vectors[i], vectors[j])
            count += 1
    return total / count if count else 0.0


def _connected_components_cosine(
    embeddings: list[list[float]],
    threshold: float,
) -> list[list[int]]:
    """Union-find over the cosine-similarity graph.

    Edge (i, j) exists iff ``cosine_similarity(embeddings[i],
    embeddings[j]) >= threshold``, equivalently
    ``cosine_distance <= 1 - threshold``. Returns components as lists
    of input indices.
    """
    n = len(embeddings)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Lower-index root wins for determinism.
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    distance_threshold = 1.0 - threshold
    for i in range(n):
        for j in range(i + 1, n):
            if _cosine_distance(embeddings[i], embeddings[j]) <= distance_threshold:
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _hdbscan_labels(
    embeddings: list[list[float]],
    *,
    min_cluster_size: int,
) -> list[int]:
    """Run HDBSCAN and return per-input cluster labels.

    Cluster ``-1`` is HDBSCAN's noise label. Labels are arbitrary
    integers that we renumber deterministically downstream.
    """
    try:
        import hdbscan  # type: ignore[import-untyped]
        import numpy as np
    except ImportError as exc:  # pragma: no cover — import gate
        raise RuntimeError(
            "hdbscan not installed. Install the optional 'embeddings' "
            "group: poetry install --with embeddings"
        ) from exc

    arr = np.asarray(embeddings, dtype=np.float64)
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        metric="euclidean",
        # mpnet embeddings are unit-normalised, so euclidean on the
        # hypersphere is monotonic in cosine distance.
        cluster_selection_method="eom",
        approx_min_span_tree=False,
    )
    labels = clusterer.fit_predict(arr)
    return [int(label) for label in labels]


def compute_narrative_clusters(
    items: Iterable[tuple[str, list[float]]],
    *,
    algorithm: Algorithm = "hdbscan",
    min_size: int = 3,
    cosine_threshold: float = 0.75,
) -> ClusteringResult:
    """Cluster ``(content_hash, embedding)`` pairs.

    Parameters
    ----------
    items
        Iterable of ``(content_hash, embedding)``. Embeddings must
        share a common dimensionality; raises if mixed.
    algorithm
        ``"hdbscan"`` (default, requires the embeddings dep group) or
        ``"cosine_threshold"`` (pure-Python fallback).
    min_size
        Minimum cluster size. Clusters smaller than this are merged
        into the outlier bucket (cluster_id = -1).
    cosine_threshold
        Threshold for the cosine_threshold algorithm only. Ignored for
        HDBSCAN. ``0.75`` is the v2a default; tune per region.

    Returns
    -------
    ClusteringResult
        ``assignments`` covers every input (including outliers);
        ``clusters`` covers only non-outlier clusters of size >=
        min_size.
    """
    if min_size < 2:
        raise ValueError("min_size must be >= 2")
    if algorithm not in ("hdbscan", "cosine_threshold"):
        raise ValueError(f"unknown algorithm: {algorithm}")
    items_list = list(items)
    if not items_list:
        return ClusteringResult(assignments=[], clusters=[])

    hashes = [h for h, _ in items_list]
    embeddings = [v for _, v in items_list]
    dims = {len(v) for v in embeddings}
    if len(dims) > 1:
        raise ValueError(f"mixed embedding dims: {sorted(dims)}")

    # HDBSCAN/KDTree quebram (`k must be <= n_training_points`) quando há
    # menos pontos que `min_size`. Janela ainda não tem volume suficiente
    # para formar cluster — todos viram noise (cluster_id = -1). Validação
    # de dims acima ainda precisa rodar para detectar entrada inválida.
    if len(items_list) < min_size:
        return ClusteringResult(
            assignments=[
                NarrativeClusterAssignment(
                    content_hash=h,
                    cluster_id=-1,
                    member_role="outlier",
                    distance_to_centroid=None,
                )
                for h, _ in items_list
            ],
            clusters=[],
        )

    if algorithm == "hdbscan":
        raw_labels = _hdbscan_labels(embeddings, min_cluster_size=min_size)
        # Renumber: gather (label → indices), drop noise label, drop
        # clusters smaller than min_size (HDBSCAN sometimes emits them
        # with eom selection), sort deterministically, assign new IDs.
        label_to_indices: dict[int, list[int]] = defaultdict(list)
        for idx, lab in enumerate(raw_labels):
            label_to_indices[lab].append(idx)
        non_noise_groups = [
            grp
            for lab, grp in label_to_indices.items()
            if lab != -1 and len(grp) >= min_size
        ]
    elif algorithm == "cosine_threshold":
        components = _connected_components_cosine(embeddings, cosine_threshold)
        non_noise_groups = [grp for grp in components if len(grp) >= min_size]
    else:  # pragma: no cover — Literal enforces at type-check time
        raise ValueError(f"unknown algorithm: {algorithm}")

    # Sort groups deterministically: larger first, ties broken by
    # member content_hashes (sorted). Same input → same cluster IDs.
    sorted_groups = sorted(
        (tuple(sorted(grp, key=lambda i: hashes[i])) for grp in non_noise_groups),
        key=lambda grp: (-len(grp), tuple(hashes[i] for i in grp)),
    )

    clusters: list[NarrativeCluster] = []
    assignments: list[NarrativeClusterAssignment | None] = [None] * len(items_list)

    for cid, grp_indices in enumerate(sorted_groups):
        member_vecs = [embeddings[i] for i in grp_indices]
        centroid = _centroid(member_vecs)
        # Find the index in grp closest to centroid → centroid member.
        # Tie-break by content_hash (sorted) so the choice is deterministic.
        best_idx = grp_indices[0]
        best_dist = _cosine_distance(embeddings[best_idx], centroid)
        for idx in grp_indices[1:]:
            d = _cosine_distance(embeddings[idx], centroid)
            if d < best_dist or (d == best_dist and hashes[idx] < hashes[best_idx]):
                best_idx = idx
                best_dist = d

        for idx in grp_indices:
            role: Literal["centroid", "member", "outlier"] = (
                "centroid" if idx == best_idx else "member"
            )
            assignments[idx] = NarrativeClusterAssignment(
                content_hash=hashes[idx],
                cluster_id=cid,
                member_role=role,
                distance_to_centroid=_cosine_distance(embeddings[idx], centroid),
            )

        clusters.append(
            NarrativeCluster(
                cluster_id=cid,
                members=tuple(hashes[i] for i in grp_indices),
                centroid=tuple(centroid),
                cluster_size=len(grp_indices),
                avg_intra_cluster_distance=_avg_pairwise_distance(member_vecs),
                algorithm=algorithm,
            )
        )

    # Anything still None is an outlier.
    for i, ass in enumerate(assignments):
        if ass is None:
            assignments[i] = NarrativeClusterAssignment(
                content_hash=hashes[i],
                cluster_id=-1,
                member_role="outlier",
                distance_to_centroid=None,
            )

    return ClusteringResult(
        assignments=[a for a in assignments if a is not None],
        clusters=clusters,
    )


__all__ = [
    "Algorithm",
    "ClusteringResult",
    "NarrativeCluster",
    "NarrativeClusterAssignment",
    "compute_narrative_clusters",
]
