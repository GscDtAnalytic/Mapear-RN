"""Pydantic models for social posts (Facebook / Instagram / X / TikTok).

The Raw layer is platform-agnostic on purpose: one table per layer
(``raw_social_posts``, ``silver_social_posts``) with ``platform`` as a
first-class column. This keeps fct_content_gold's cross-source union
trivial — the same pattern the Silver schema already uses for
``source_type`` ∈ {rss, social}.

``post_id`` is the merge key for BQLoader.load(merge_key='post_id');
Apify's per-platform IDs are already globally unique within a platform,
so we prefix with the platform to avoid collisions in the unified table
(e.g. ``fb:1234567890``, ``ig:abcdef``, ``x:9876543210``).

Stage 1B closed G-02: ``SilverSocialPost`` and ``SocialPostDLQ`` were
added so the codegen has Pydantic source-of-truth for all three social
warehouse tables. The pipeline still constructs flat dicts on the way
into Parquet; the Pydantic models describe the shape and let the drift
test exercise the full Pydantic↔Arrow↔BQ JSON ladder.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, computed_field

from mapear_domain.models.base import ScopeStatusLiteral, SentimentLabel
from mapear_domain.schemas import DecisionFactor, EntityRef, EntitySentiment

Platform = Literal["facebook", "instagram", "x", "tiktok"]


class SocialAccount(BaseModel):
    """Author/page identity. One row in dim_social_accounts downstream."""

    platform: Platform
    handle: str  # page slug (FB), username (IG), screen_name (X)
    display_name: str = ""
    verified: bool = False


class Engagement(BaseModel):
    """Per-post engagement counters at extraction time.

    Apify may return None for any counter when the target is private,
    deleted, or the actor cannot read it — so every field is nullable.
    Downstream SQL treats NULL as 0 for rollups, but we preserve NULL
    here to keep "actor could not read" distinct from "legitimately 0".
    """

    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None


class SocialPost(BaseModel):
    """Raw social-media post — one row per Apify dataset item.

    Mirrors ``RawArticle`` for fields shared with RSS so the
    ETL boilerplate (dedup, MERGE, freshness) stays uniform.
    """

    # --- Identity / merge key ---
    post_id: str  # prefixed: "fb:<id>" / "ig:<id>" / "x:<id>"
    platform: Platform
    url: HttpUrl

    # --- Authorship ---
    account: SocialAccount
    author_display_name: str = ""

    # --- Content ---
    text: str = ""
    language: str | None = None

    # --- Temporal ---
    published_at: datetime
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # --- Engagement ---
    engagement: Engagement = Field(default_factory=Engagement)

    # --- Platform-specific flags ---
    is_repost: bool = False  # retweet on X, shared post on FB
    is_reply: bool = False  # quote tweet / comment reply
    parent_post_id: str | None = None  # when is_repost / is_reply

    # --- Lineage / governance ---
    content_hash: str
    actor_run_id: str
    ingestion_run_id: str
    schema_version: int = 1
    # source_type is always "social"; ``platform`` narrows it to FB/IG/X/TikTok.
    source_type: str = "social"
    # Tenant identifier — stamped from settings.mapear_tenant_id
    # (Stage 2B v1, data plane). None for single-tenant / legacy rows.
    tenant_id: str | None = None


class SilverSocialPost(BaseModel):
    """Cleaned + enriched social post — one row in ``silver_social_posts``.

    Flattens the ``account`` and ``engagement`` structs from the raw layer
    so downstream SQL can group/filter without ``UNNEST``. Adds NER,
    sentiment, electoral overlay, and identity-resolution fields.

    Field order mirrors the deployed BQ schema; not all producers populate
    every Optional column.
    """

    # --- Identity / merge key ---
    post_id: str
    platform: Platform
    url: HttpUrl

    # --- Authorship (flattened from SocialAccount) ---
    author_handle: str
    author_display_name: str | None = None
    author_verified: bool

    # --- Content ---
    text: str
    language: str | None = None
    language_confidence: float | None = None
    language_reason: str | None = None

    # --- Temporal ---
    published_at: datetime
    extracted_at: datetime

    # --- Engagement (flattened from Engagement) ---
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    views: int | None = None

    # --- Platform-specific flags ---
    is_repost: bool
    is_reply: bool
    parent_post_id: str | None = None

    # --- NER ---
    entities: list[EntityRef] = Field(default_factory=list)
    mentioned_cities: list[str] = Field(default_factory=list)
    mentioned_mayors: list[str] = Field(default_factory=list)
    mentioned_governors: list[str] = Field(default_factory=list)
    mentioned_parties: list[str] = Field(default_factory=list)
    mentioned_candidates: list[str] = Field(default_factory=list)
    mentioned_politicians: list[str] = Field(default_factory=list)
    mentioned_persons: list[str] = Field(default_factory=list)
    is_rn_relevant: bool

    # --- Sentiment ---
    sentiment_overall: float | None = None
    sentiment_by_entity: list[EntitySentiment] = Field(default_factory=list)

    # --- Electoral overlay ---
    person_id: str | None = None
    scope_status: ScopeStatusLiteral | None = None
    resolution_confidence: float | None = None
    sentiment_label: SentimentLabel | None = None
    confidence_score: float | None = None
    risk_score: float | None = None
    decision_factors: list[DecisionFactor] = Field(default_factory=list)

    # --- Lineage / governance ---
    content_hash: str
    actor_run_id: str
    ingestion_run_id: str
    rule_version: str | None = None
    model_version: str | None = None
    pipeline_version: str | None = None
    source_type: str = "social"
    batch_id: str

    # --- Identity-resolution (Phase 2) ---
    author_base_city: str | None = None
    data_type: str | None = None
    effective_cutoff_date: datetime | None = None
    identity_resolution_version: str | None = None
    # Tenant identifier — see SocialPost.tenant_id.
    tenant_id: str | None = None

    # --- Eixo 2 v1 — LLM narrative summary ---
    narrative_summary: str | None = None
    narrative_prompt_version: str | None = None

    # --- V1 canonical computed fields (mirror SilverArticle / GoldArticle) ---
    @computed_field  # type: ignore[misc]
    @property
    def content_rn_relevant(self) -> bool:
        """Canonical rename of is_rn_relevant: True when content mentions RN."""
        return self.is_rn_relevant

    @computed_field  # type: ignore[misc]
    @property
    def author_in_scope(self) -> bool:
        """Canonical boolean from scope_status. True when author is IN_SCOPE."""
        return self.scope_status == "IN_SCOPE"


class SilverAuthorActivation(BaseModel):
    """One row per (author, person-target) activation — Eixo 3 v1.

    Foundation table for the author co-activation graph (CIB detection).
    A "social post that mentions a politician" is fanned out into one
    activation row per mentioned person. Two authors that produce
    activations against the same ``person_target`` within
    ``MAPEAR_CIB_WINDOW_HOURS`` become candidates for coordination.

    Grain: (author_id, platform, content_hash, person_target,
    published_at). One Silver post that mentions N politicians yields N
    rows here; an author handle is the surrogate key for v1 (no cross-
    platform identity resolution yet — that is the v2 deliverable, see
    ADR docs/decisions/adr-eixo-3-v1-coactivation-graph.md).

    ``person_target`` carries the raw mentioned-name string (e.g. "Fátima
    Bezerra"); resolution to a stable ``person_id`` happens upstream in
    the silver pipeline and is *not* required here — the activation
    table is intentionally upstream of identity resolution so it can
    surface coordination against not-yet-resolved targets.
    """

    # --- Identity / merge key ---
    author_id: str  # = SilverSocialPost.author_handle (surrogate, v1)
    platform: Platform
    post_id: str  # lineage back to silver_social_posts
    content_hash: str
    person_target: str  # one of the mentioned_* names in the source post
    published_at: datetime

    # --- Target context ---
    # Which mentioned_* list the target came from. Useful for filtering
    # coordination by office (mayors vs governors vs generic persons).
    target_kind: Literal[
        "mayor", "governor", "candidate", "politician", "party", "person"
    ]
    # Resolved person_id when the upstream resolver has matched the target
    # to a known entity, else None. v1 graph keys on the raw
    # ``person_target`` string, not on ``person_id``, so coordination
    # signal does not depend on resolver coverage.
    target_person_id: str | None = None

    # --- Author / governance carry from the source post ---
    author_in_scope: bool | None = None
    extracted_at: datetime
    batch_id: str
    actor_run_id: str | None = None
    ingestion_run_id: str | None = None
    pipeline_version: str | None = None
    schema_version: int = 1
    source_type: str = "social"
    # Region 2A — the Region slug the activation belongs to (e.g. "rn").
    # None for pre-Region-DI rows.
    region: str | None = None
    # Tenant identifier — see SocialPost.tenant_id.
    tenant_id: str | None = None


class SilverAuthorCommunity(BaseModel):
    """Daily community assignment for an author — Eixo 3 v2a.

    Written by the out-of-band community-detection job
    (``mapear_nlp.graph.run_community_detection``). One row per
    (activation_date, region, author_id, platform, algorithm) — re-
    running the job with a different algorithm produces additional
    rows rather than overwriting; analysts can compare Louvain vs
    label-propagation side by side.

    The ``community_id`` is **not** globally stable across days. Louvain
    is order-deterministic given a seed, but adding a new edge in
    tomorrow's graph can renumber. The downstream mart
    ``fct_author_community_daily`` keys joins on
    ``(activation_date, region, algorithm, community_id)``; cross-day
    cluster tracking is a v3 deliverable (cluster-identity persistence).
    """

    # --- Grain ---
    activation_date: datetime  # truncated to day in UTC by the job
    region: str | None
    author_id: str
    platform: Platform
    algorithm: Literal["louvain", "label_propagation"]
    community_id: int

    # --- Cluster-level rollup (denormalised onto every member row so
    # downstream queries don't need a self-join) ---
    community_size: int
    edge_count: int
    edge_density: float
    avg_co_post_count: float
    avg_jaccard: float

    # --- Job lineage ---
    job_run_id: str
    run_at: datetime
    pipeline_version: str
    schema_version: int = 1
    source_type: str = "social"
    # Tenant identifier — see SocialPost.tenant_id.
    tenant_id: str | None = None


class SilverAuthorPersona(BaseModel):
    """Cross-platform author identity — Eixo 3 v2b.

    One row per ``(persona_id, platform, author_id)``: a persona that
    spans two or more platforms produces N rows. Written by the
    out-of-band author-resolution job
    (``mapear_nlp.graph.run_author_resolution``) and consumed by the
    CIB graph **opt-in** via ``MAPEAR_CIB_USE_PERSONAS`` — defaulting
    to off so v1+v2a outputs are unchanged.

    ``persona_id`` is content-addressed: ``sha1`` over the sorted
    member tuple. Identical inputs always produce identical ids, but
    a persona's id will change if its member set changes — a new
    platform joining the persona renumbers it. Cross-day persona
    persistence (stitching newly-merged accounts back to yesterday's
    persona_id) is a v3 deliverable.

    Grain: ``(persona_id, platform, author_id)``. The same author
    appearing on a third platform tomorrow lands as a new row with the
    *new* persona_id; the previous-day persona stays as-is.

    Anti-objective: ``SilverAuthorPersona`` does NOT supersede
    ``SilverAuthorActivation``. Activations remain keyed on
    ``(author_id, platform, ...)``; this table is the **join key**
    used by downstream graph code when the operator opts in.
    """

    # --- Grain ---
    persona_id: str  # sha1[:16] of sorted member tuple
    platform: Platform
    author_id: str  # = SilverSocialPost.author_handle (v1 surrogate)

    # --- Persona-level rollup (denormalized onto every member row) ---
    member_count: int
    canonical_handle: str
    canonical_display_name: str | None = None
    confidence: float  # weakest pairwise match-edge inside the cluster
    resolution_version: str  # IDENTITY_RESOLUTION_AUTHOR_VERSION

    # --- Pairwise evidence (denormalized as JSON-encoded blob) ---
    # The full PairScore list isn't shaped like a Pydantic-friendly
    # array of structs — different evidence fields are nullable per
    # signal availability. Serialising as a string keeps the BQ schema
    # simple; downstream SQL can JSON_EXTRACT when needed.
    evidence_json: str | None = None

    # --- Job lineage ---
    activation_date: datetime  # truncated to day in UTC by the job
    job_run_id: str
    run_at: datetime
    pipeline_version: str
    schema_version: int = 1
    source_type: str = "social"
    # Region 2A — the Region slug the persona belongs to.
    region: str | None = None
    # Tenant identifier — see SocialPost.tenant_id.
    tenant_id: str | None = None


class SilverCommunityScore(BaseModel):
    """Inauthenticity score for a detected community — Eixo 3 v3.

    Written by the out-of-band community-detection job alongside
    ``SilverAuthorCommunity``. One row per
    (activation_date, region, algorithm, community_id).

    The composite score is a weighted combination of:
      - synchrony (normalised co_post_count)
      - alignment (Jaccard over lifetime target sets)
      - content similarity (average cosine sim between post embeddings)

    All score components are in [0, 1]. ``composite_score`` is the
    weighted aggregate; when content similarity embeddings are not
    available, ``avg_content_similarity`` is None and the weight is
    redistributed over the other two terms.
    """

    # --- Grain ---
    activation_date: datetime  # truncated to day in UTC
    region: str | None
    algorithm: Literal["louvain", "label_propagation"]
    community_id: int

    # --- Dimensions ---
    community_size: int
    pair_count: int

    # --- Score components ---
    avg_synchrony_score: float
    avg_alignment_score: float
    avg_content_similarity_score: float | None
    composite_score: float

    # --- Reproducibility ---
    score_version: str  # e.g. "v1"
    score_weights_json: str  # JSON of ScoringWeights for auditability

    # --- Job lineage ---
    job_run_id: str
    run_at: datetime
    pipeline_version: str
    schema_version: int = 1
    source_type: str = "social"
    tenant_id: str | None = None


class SilverClusterSeries(BaseModel):
    """Cross-day cluster-identity persistence — Eixo 3 v3.

    Written by the out-of-band community-detection job. One row per
    (activation_date, region, algorithm, community_id).

    A ``series_id`` is a stable SHA1[:16] derived from the first-day
    membership of the cluster. Two community-detection runs on adjacent
    days that produce clusters with Jaccard overlap ≥ threshold are
    assigned the same ``series_id`` — they are considered "the same
    squad". A cluster that appears for the first time (or reappears
    after a gap) starts a new series.
    """

    # --- Grain ---
    activation_date: datetime  # truncated to day in UTC
    region: str | None
    algorithm: Literal["louvain", "label_propagation"]
    community_id: int

    # --- Series identity ---
    series_id: str  # sha1[:16] from initial member set
    series_start_date: datetime  # UTC day-truncated
    jaccard_to_previous: float | None  # None for series' first appearance
    is_new_series: bool

    # --- Job lineage ---
    job_run_id: str
    run_at: datetime
    pipeline_version: str
    schema_version: int = 1
    source_type: str = "social"
    tenant_id: str | None = None


class SilverSocialPostEmbedding(BaseModel):
    """Embedding vector for one social post — Eixo 2 v2a social.

    Written by the out-of-band social embedding job
    (``mapear_nlp.graph.run_social_embedding``). Input text is
    ``SilverSocialPost.text`` — the raw post text, not the LLM-generated
    ``narrative_summary``. Using raw text (rather than the narrative
    summary) ensures coverage across all posts, not just the ~5% that
    reach the ALERT threshold.

    The vector is used by the community-detection job to populate
    ``AuthorPair.avg_content_similarity`` in Eixo 3 v3, closing the
    gap where all social pairs scored ``content_similarity = None``.

    Grain: ``(content_hash, embedding_model)``. Re-embedding with a new
    model adds rows next to the old ones without invalidating prior
    vectors — downstream consumers can pin a model version.
    """

    # --- Grain ---
    content_hash: str
    embedding_model: str  # e.g. "paraphrase-multilingual-mpnet-base-v2"

    # --- Vector payload ---
    embedding_dim: int
    embedding: list[float]

    # --- Job lineage ---
    job_run_id: str
    run_at: datetime
    pipeline_version: str
    schema_version: int = 1
    source_type: str = "social"
    region: str | None = None
    tenant_id: str | None = None


class SocialPostDLQ(BaseModel):
    """Dead-letter queue entry for unparseable Apify items.

    Written to ``raw_social_posts_dlq`` when an adapter fails to map an
    Apify dataset item to a ``SocialPost``. ``raw_payload_json`` carries
    the original payload verbatim (JSON-encoded) so the operator can
    replay or repair it after fixing the adapter.

    ``platform`` is ``str`` rather than ``Platform`` because DLQ rows can
    arrive from unknown / future actors that the literal does not yet
    enumerate — the DLQ should never reject input on a typed enum.
    """

    ingestion_run_id: str
    platform: str
    actor_id: str
    actor_run_id: str
    error_type: str
    error_message: str
    raw_payload_json: str
    raw_keys_json: str | None = None
    created_at: datetime
    # Tenant identifier — see SocialPost.tenant_id.
    tenant_id: str | None = None
