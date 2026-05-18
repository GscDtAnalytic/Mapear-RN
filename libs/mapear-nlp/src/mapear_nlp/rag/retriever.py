"""BQ VECTOR_SEARCH retriever — Eixo 2 v2c.

Embeds a free-text query with the same sentence-transformer used by the
clustering job, then issues a VECTOR_SEARCH query over
``silver_narrative_embeddings`` to find the top-k semantically similar
narratives.  Results are enriched with cluster membership and stance
labels via LEFT JOINs so the generator has full context without extra
round-trips.

The query vector is serialised as a literal ``ARRAY<FLOAT64>`` in the SQL
string.  BigQuery's Jobs API does not support ARRAY typed parameters, so
parameterisation is not an option here.  The embedding values are
floating-point numbers produced by a deterministic encoder — no
user-controlled input reaches the SQL string.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mapear_nlp.embeddings.client import EmbeddingClient


@dataclass(frozen=True)
class NarrativeHit:
    """One retrieved narrative, enriched from cluster and stance tables."""

    content_hash: str
    narrative_summary: str
    distance: float
    published_at: datetime | None = None
    person_id: str | None = None
    person_name: str | None = None
    person_role: str | None = None
    cluster_id: int | None = None
    cluster_size: int | None = None
    cluster_label: str | None = None
    stance_label: str | None = None
    stance_confidence: str | None = None


def _build_sql(
    *,
    project: str,
    silver_ds: str,
    gold_ds: str,
    embedding_model: str,
    embedding: list[float],
    region: str | None,
    k: int,
) -> str:
    embedding_literal = ", ".join(repr(v) for v in embedding)
    region_emb = f"AND region = '{region}'" if region else ""
    region_nc = f"AND region = '{region}'" if region else ""

    return f"""
SELECT
  base.content_hash,
  ga.narrative_summary,
  ga.published_at,
  ga.person_id,
  sa.person_name,
  sa.person_role,
  nc.cluster_id,
  nc.cluster_size,
  nc.cluster_label,
  sa.stance_label,
  sa.confidence AS stance_confidence,
  distance
FROM VECTOR_SEARCH(
  (
    SELECT content_hash, embedding
    FROM `{project}.{silver_ds}.silver_narrative_embeddings`
    WHERE embedding_model = '{embedding_model}'
      {region_emb}
  ),
  'embedding',
  (SELECT [{embedding_literal}] AS embedding),
  top_k => {k},
  distance_type => 'COSINE'
)
LEFT JOIN `{project}.{gold_ds}.gold_articles` ga
  ON base.content_hash = ga.content_hash
LEFT JOIN (
  SELECT content_hash, cluster_id, cluster_size, cluster_label
  FROM `{project}.{silver_ds}.silver_narrative_clusters`
  WHERE algorithm = 'hdbscan' AND cluster_id >= 0
    {region_nc}
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY content_hash ORDER BY cluster_run_date DESC
  ) = 1
) nc ON base.content_hash = nc.content_hash
LEFT JOIN (
  SELECT content_hash, stance_label, confidence, person_name, person_role
  FROM `{project}.{silver_ds}.silver_article_stances`
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY content_hash ORDER BY classified_at DESC
  ) = 1
) sa ON base.content_hash = sa.content_hash
ORDER BY distance ASC
"""


def retrieve(
    query_text: str,
    *,
    embedding_client: EmbeddingClient,
    bq_client: Any,
    project: str,
    silver_ds: str,
    gold_ds: str,
    embedding_model: str,
    region: str | None = None,
    k: int = 5,
) -> list[NarrativeHit]:
    """Embed *query_text* and return the top-*k* similar narratives from BQ.

    Args:
        query_text: Natural-language question (pt-BR).
        embedding_client: Sentence-transformer client — must use the same
            model as the stored embeddings; mismatching produces nonsense.
        bq_client: ``google.cloud.bigquery.Client``.
        project: GCP project ID.
        silver_ds: BQ silver dataset name.
        gold_ds: BQ gold dataset name.
        embedding_model: Model string stored in
            ``silver_narrative_embeddings.embedding_model``.
        region: Optional region slug filter (e.g. ``"rn"``).
            ``None`` searches all regions.
        k: Number of nearest neighbours to return.

    Returns:
        List of :class:`NarrativeHit` ordered by ascending distance
        (closest / most similar first).
    """
    vectors = embedding_client.encode([query_text])
    query_embedding = vectors[0]

    sql = _build_sql(
        project=project,
        silver_ds=silver_ds,
        gold_ds=gold_ds,
        embedding_model=embedding_model,
        embedding=query_embedding,
        region=region,
        k=k,
    )

    rows = list(bq_client.query(sql).result())
    return [
        NarrativeHit(
            content_hash=row["content_hash"],
            narrative_summary=row["narrative_summary"] or "",
            distance=float(row["distance"]),
            published_at=row.get("published_at"),
            person_id=row.get("person_id"),
            person_name=row.get("person_name"),
            person_role=row.get("person_role"),
            cluster_id=row.get("cluster_id"),
            cluster_size=row.get("cluster_size"),
            cluster_label=row.get("cluster_label"),
            stance_label=row.get("stance_label"),
            stance_confidence=row.get("stance_confidence"),
        )
        for row in rows
    ]
