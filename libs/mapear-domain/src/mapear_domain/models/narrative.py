"""Pydantic schemas for narrative analysis tables — Eixo 2 v2a/v2b.

These tables sit downstream of ``GoldArticle.narrative_summary`` (the
LLM-as-explainer output from Eixo 2 v1) and are written by out-of-band jobs:
- ``mapear_nlp.clustering.run_narrative_clustering`` (v2a — embeddings + clusters)
- ``mapear_nlp.run_stance_classification`` (v2b — stance labels)

Why two tables and not one
--------------------------
``SilverNarrativeEmbedding`` is keyed by ``(content_hash,
embedding_model)`` — the vector itself. Rotating the model (e.g. from
``paraphrase-multilingual-mpnet-base-v2`` to a larger LaBSE variant)
adds new rows next to the old ones without invalidating prior work.

``SilverNarrativeCluster`` is keyed by ``(cluster_run_date, region,
algorithm, content_hash)`` — the cluster assignment for one article
on one day under one algorithm. Re-running with a different algorithm
(HDBSCAN vs cosine_threshold) produces additional rows rather than
overwriting; analysts can compare clusterings side by side.

Cluster IDs are NOT stable across days — HDBSCAN renumbers when new
articles land in the embedding space. Downstream mart
``fct_narrative_cluster_daily`` keys joins on
``(cluster_run_date, region, algorithm, cluster_id)``; cross-day cluster
tracking (narrative-identity persistence) is a v3 deliverable.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SilverNarrativeEmbedding(BaseModel):
    """One embedding vector per (content_hash, embedding_model).

    The embedding is computed from ``GoldArticle.narrative_summary`` —
    the 2-4 sentence Portuguese summary emitted by the Eixo 2 v1
    explainer. Articles without a narrative_summary (non-ALERT rows)
    are not embedded.

    Grain: ``(content_hash, embedding_model)``. Re-embedding with a
    new model lands as a new row; old vectors remain queryable so
    downstream consumers can pin a model version. The clustering job
    reads only one model at a time (the one in
    ``MAPEAR_EMBEDDINGS_MODEL``).
    """

    # --- Grain ---
    content_hash: str
    embedding_model: str  # e.g. "paraphrase-multilingual-mpnet-base-v2"

    # --- Vector payload ---
    embedding_dim: int
    embedding: list[float]

    # --- Lineage from the narrative that produced this vector ---
    # Pins the (rule_version, prompt_version) that produced the
    # underlying narrative_summary. A bump in either invalidates the
    # cache key in narrative_cache; the embedding stays valid here
    # because the vector is a function of the narrative text, not of
    # the prompt that produced it. Kept for audit.
    narrative_prompt_version: str | None = None
    rule_version: str | None = None

    # --- Job lineage ---
    job_run_id: str
    run_at: datetime
    pipeline_version: str
    schema_version: int = 1
    source_type: str = "rss"
    # Region 2A — the Region slug the embedding belongs to.
    region: str | None = None
    # Tenant identifier — see RawArticle.tenant_id.
    tenant_id: str | None = None


class SilverNarrativeCluster(BaseModel):
    """Daily cluster assignment for one narrative — Eixo 2 v2a.

    Written by the out-of-band clustering job
    (``mapear_nlp.clustering.run_narrative_clustering``). One row per
    ``(cluster_run_date, region, algorithm, content_hash)``: re-running
    the job with a different algorithm produces additional rows rather
    than overwriting.

    ``cluster_id`` semantics:
      * non-negative int → cluster member (deterministic, sorted by
        member tuple within a (date, region, algorithm) partition).
      * ``-1`` → outlier / noise. HDBSCAN convention; preserved so
        downstream queries can filter ``cluster_id >= 0`` for "in a
        cluster" vs ``cluster_id = -1`` for "lonely narrative".

    Cluster IDs are NOT globally stable across days. New articles
    shift the embedding-space density; the mart layer keys joins on
    ``(cluster_run_date, region, algorithm, cluster_id)``. Cross-day
    cluster-identity persistence is a v3 deliverable.
    """

    # --- Grain ---
    cluster_run_date: datetime  # truncated to day in UTC by the job
    region: str | None
    algorithm: Literal["hdbscan", "cosine_threshold"]
    content_hash: str

    # --- Cluster assignment ---
    embedding_model: str
    cluster_id: int  # -1 means outlier / noise
    member_role: Literal["centroid", "member", "outlier"]

    # --- Cluster-level rollup (denormalised onto every member row so
    # downstream queries don't need a self-join) ---
    cluster_size: int
    distance_to_centroid: float | None = None  # None for outliers
    avg_intra_cluster_distance: float | None = None
    cluster_label: str | None = None  # short top-terms summary

    # --- Job lineage ---
    job_run_id: str
    run_at: datetime
    pipeline_version: str
    schema_version: int = 1
    source_type: str = "rss"
    # Tenant identifier — see RawArticle.tenant_id.
    tenant_id: str | None = None


class SilverArticleStance(BaseModel):
    """Stance label for one narrative — Eixo 2 v2b.

    Written by the out-of-band stance-classification job
    (``mapear_nlp.run_stance_classification``). One row per
    ``(content_hash, stance_prompt_version)``: re-running with a new prompt
    version adds rows rather than overwriting.

    ``stance_label`` semantics:
      * ``"favor"``  — narrative presents the official positively / approvingly.
      * ``"contra"`` — narrative presents the official negatively / critically.
      * ``"neutro"`` — narrative is factual / balanced / no clear position.
      * ``None``     — classification failed (see ``error``).

    The classifier gates on ``narrative_summary IS NOT NULL``. Since v1
    (LLM-as-explainer) already gates on ``sentiment_label = ALERT``, stance
    runs on the same ~5% of rows — no additional API cost multiplier.

    Grain: ``(content_hash, stance_prompt_version)``. A prompt version bump
    reruns the full corpus without touching prior labels; analysts can compare
    prompt iterations side-by-side.
    """

    # --- Grain ---
    content_hash: str
    stance_prompt_version: str  # e.g. "stance_v1"

    # --- Stance output ---
    # None when the LLM call failed or returned unparseable JSON.
    stance_label: Literal["favor", "contra", "neutro"] | None = None
    # Confidence reported by the model ("high"|"medium"|"low"). None on error.
    confidence: Literal["high", "medium", "low"] | None = None

    # --- LLM lineage ---
    stance_model: str  # model ID used for this classification
    cache_hit: bool = False
    error: str | None = None  # populated when stance_label is None
    redaction_level: str = "masked"  # PII redaction applied to narrative

    # --- Carry from GoldArticle (avoid JOIN in most downstream queries) ---
    person_id: str | None = None
    person_name: str | None = None
    person_role: str | None = None

    # --- Job lineage ---
    classified_at: datetime
    job_run_id: str
    pipeline_version: str
    schema_version: int = 1
    source_type: str = "rss"
    # Region 2A — slug the stance belongs to (e.g. "rn").
    region: str | None = None
    # Tenant identifier — see RawArticle.tenant_id.
    tenant_id: str | None = None
