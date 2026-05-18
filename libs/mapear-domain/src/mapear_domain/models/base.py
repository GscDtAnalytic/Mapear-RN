"""Pydantic schemas for data validation across pipeline stages."""

from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, computed_field

from mapear_domain.schemas import DecisionFactor, EntityRef, EntitySentiment

SentimentLabel = Literal["FAVORABLE", "WARNING", "ALERT"]
ScopeStatusLiteral = Literal["IN_SCOPE", "OUT_OF_SCOPE", "AMBIGUOUS"]


class TopicIdSource(str, Enum):
    """Discriminator for the semantic regime that produced a topic_id value.

    GCP_ORDINAL: ordinal index from GCP classify_text response (not stable).
    KEYWORD_MAP: stable ID 1-10 from TOPIC_ID_MAP in classify_by_keywords.
    UNCLASSIFIED: both classifiers returned -1 (no topic match).
    LEGACY_UNKNOWN: records ingested before TDT-TOPIC-01 cutover; origin
        unknown. topic_label_raw is always NULL for this source. See
        docs/decisions/adr_tdt_topic_01_remediation.md.
    """

    GCP_ORDINAL = "gcp_ordinal"
    KEYWORD_MAP = "keyword_map"
    UNCLASSIFIED = "unclassified"
    LEGACY_UNKNOWN = "legacy_unknown"


class FeedSource(BaseModel):
    """A feed source to monitor."""

    name: str
    url: HttpUrl
    category: str = "general"
    priority: int = Field(default=0, ge=0, le=10)
    is_rn_focused: bool = False


class DiscoveredURL(BaseModel):
    """A URL discovered from feeds or sitemaps."""

    url: HttpUrl
    source_feed: str
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    title: str | None = None
    published_at: datetime | None = None


class RawArticle(BaseModel):
    """Raw article/content extracted from a web page or API."""

    url: HttpUrl
    source_feed: str
    title: str
    content: str
    author: str | None = None
    published_at: datetime | None = None
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    content_hash: str
    html_lang: str | None = None
    source_type: str = "rss"
    schema_version: int = 1
    # Lineage / governance — populated by ETLs that emit them.
    actor_run_id: str | None = None
    ingestion_run_id: str | None = None
    # Tenant identifier — stamped from settings.mapear_tenant_id (Stage 2B
    # v1, data plane). None for single-tenant / legacy rows.
    tenant_id: str | None = None


class SilverArticle(BaseModel):
    """Cleaned and deduplicated content with NER annotations."""

    url: HttpUrl
    source_feed: str
    title: str
    content_clean: str
    author: str | None = None
    published_at: datetime | None = None
    extracted_at: datetime
    content_hash: str
    entities: list[EntityRef] = Field(default_factory=list)
    mentioned_cities: list[str] = Field(default_factory=list)
    mentioned_mayors: list[str] = Field(default_factory=list)
    mentioned_governors: list[str] = Field(default_factory=list)
    mentioned_parties: list[str] = Field(default_factory=list)
    mentioned_persons: list[str] = Field(default_factory=list)
    # DEPRECATED (V2): use content_rn_relevant
    is_rn_relevant: bool = False
    source_type: str = "rss"
    schema_version: int = 1
    # Electoral pivot fields — None when not yet resolved (RSS legacy compat).
    person_id: str | None = None
    # DEPRECATED (V2): use author_in_scope
    scope_status: ScopeStatusLiteral | None = None
    resolution_confidence: float | None = None
    actor_run_id: str | None = None
    ingestion_run_id: str | None = None
    rule_version: str | None = None
    pipeline_version: str | None = None
    # Tenant identifier — see RawArticle.tenant_id.
    tenant_id: str | None = None

    @computed_field  # type: ignore[misc]
    @property
    def content_rn_relevant(self) -> bool:
        """Canonical rename of is_rn_relevant: True when content mentions RN topics."""
        return self.is_rn_relevant

    @computed_field  # type: ignore[misc]
    @property
    def author_in_scope(self) -> bool:
        """Canonical boolean from scope_status. True when author is IN_SCOPE."""
        return self.scope_status == "IN_SCOPE"


class GoldArticle(BaseModel):
    """Enriched content with sentiment and topic analysis."""

    url: HttpUrl
    source_feed: str
    title: str
    content_clean: str
    published_at: datetime | None = None
    content_hash: str
    # DEPRECATED (V2): use content_rn_relevant
    is_rn_relevant: bool
    mentioned_cities: list[str] = Field(default_factory=list)
    mentioned_mayors: list[str] = Field(default_factory=list)
    mentioned_governors: list[str] = Field(default_factory=list)
    mentioned_parties: list[str] = Field(default_factory=list)
    mentioned_persons: list[str] = Field(default_factory=list)
    sentiment_overall: float | None = None
    sentiment_by_entity: list[EntitySentiment] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    topic_id: int | None = None
    topic_label: str = ""
    topic_id_source: TopicIdSource | None = None
    topic_label_raw: str | None = None
    trend_score: float | None = None
    source_type: str = "rss"
    schema_version: int = 2
    # Electoral overlay — populated when classifier is enabled.
    person_id: str | None = None
    # DEPRECATED (V2): use author_in_scope
    scope_status: ScopeStatusLiteral | None = None
    sentiment_label: SentimentLabel | None = None
    confidence_score: float | None = None
    risk_score: float | None = None
    decision_factors: list[DecisionFactor] = Field(default_factory=list)
    # Eixo 2 v1 — LLM-generated narrative summary, populated only for
    # sentiment_label=ALERT rows. None for WARNING/FAVORABLE and for
    # rows ingested before Eixo 2 v1 (cost-gated). See ADR
    # docs/decisions/adr-eixo-2-v1-llm-explainer.md.
    narrative_summary: str | None = None
    # Prompt version that produced narrative_summary — pins the cache
    # key so swapping the prompt forces a fresh LLM call.
    narrative_prompt_version: str | None = None
    # Lineage / governance.
    actor_run_id: str | None = None
    ingestion_run_id: str | None = None
    rule_version: str | None = None
    model_version: str | None = None
    pipeline_version: str | None = None
    processed_at_utc: datetime | None = None
    # Tenant identifier — see RawArticle.tenant_id.
    tenant_id: str | None = None

    @computed_field  # type: ignore[misc]
    @property
    def content_rn_relevant(self) -> bool:
        """Canonical rename of is_rn_relevant: True when content mentions RN topics."""
        return self.is_rn_relevant

    @computed_field  # type: ignore[misc]
    @property
    def author_in_scope(self) -> bool:
        """Canonical boolean from scope_status. True when author is IN_SCOPE."""
        return self.scope_status == "IN_SCOPE"
