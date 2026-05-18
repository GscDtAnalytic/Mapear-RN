"""Per-table contract config for the social warehouse tables.

Counterpart to ``mapear_storage.contracts.ARTICLE_CONTRACTS`` — same
``TableContract`` dataclass, same knobs, scoped to social. Lives here
(not in ``mapear-storage``) so that mapear-storage does not need to
import ``mapear_social.models`` at module load time. Production images
that don't ship the social pipeline (e.g. the RSS Cloud Run image) can
still import ``mapear_storage.loaders.parquet_writer`` without a
mapear-social dependency.

The ``parquet_writer`` module imports this lazily inside try/except so
that ``SOCIAL_RAW_SCHEMA`` / ``SOCIAL_SILVER_SCHEMA`` / ``SOCIAL_DLQ_SCHEMA``
are populated when mapear-social is installed and ``None`` otherwise.

Closes G-02 (silver_social_posts and raw_social_posts_dlq had no
dedicated Pydantic model). After Stage 1B, the drift test exercises
the full Pydantic↔Arrow↔BQ JSON ladder for all three social tables.
"""

from __future__ import annotations

from mapear_social.models import (
    SilverAuthorActivation,
    SilverAuthorCommunity,
    SilverAuthorPersona,
    SilverClusterSeries,
    SilverCommunityScore,
    SilverSocialPost,
    SilverSocialPostEmbedding,
    SocialPost,
    SocialPostDLQ,
)
from mapear_storage.contracts import TableContract

# V1 canonical computed bool fields — match the convention in
# mapear_storage.contracts._V1_NULLABLE.
_V1_NULLABLE = frozenset({"content_rn_relevant", "author_in_scope"})


SOCIAL_CONTRACTS: dict[str, TableContract] = {
    "raw_social_posts": TableContract(
        pydantic=SocialPost,
        # `author_display_name` defaults to "" but BQ accepts NULL for legacy rows.
        nullable_overrides=frozenset({"author_display_name"}),
    ),
    "silver_social_posts": TableContract(
        pydantic=SilverSocialPost,
        nullable_overrides=_V1_NULLABLE,
    ),
    "raw_social_posts_dlq": TableContract(
        pydantic=SocialPostDLQ,
    ),
    # Eixo 3 v1 — author co-activation foundation table.
    "silver_author_activations": TableContract(
        pydantic=SilverAuthorActivation,
    ),
    # Eixo 3 v2a — community assignment from out-of-band detection job.
    "silver_author_communities": TableContract(
        pydantic=SilverAuthorCommunity,
    ),
    # Eixo 3 v2b — cross-platform author identity (persona) from the
    # out-of-band author-resolution job.
    "silver_author_personas": TableContract(
        pydantic=SilverAuthorPersona,
    ),
    # Eixo 3 v3 — inauthenticity composite score per community.
    "silver_community_scores": TableContract(
        pydantic=SilverCommunityScore,
        nullable_overrides=frozenset({"avg_content_similarity_score"}),
    ),
    # Eixo 3 v3 — cross-day cluster-identity series assignment.
    "silver_cluster_series": TableContract(
        pydantic=SilverClusterSeries,
        nullable_overrides=frozenset({"jaccard_to_previous"}),
    ),
    # Eixo 2 v2a social — embedding vector for raw social post text.
    # Used by the community-detection job to populate content_similarity
    # in the Eixo 3 v3 inauthenticity score. Grain: (content_hash,
    # embedding_model) — same as silver_narrative_embeddings for RSS.
    "silver_social_post_embeddings": TableContract(
        pydantic=SilverSocialPostEmbedding,
    ),
}


__all__ = ["SOCIAL_CONTRACTS"]
