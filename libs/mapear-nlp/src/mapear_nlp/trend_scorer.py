"""Trend scoring based on publication volume, recency, and velocity.

Computes a composite trend score per entity/topic that reflects:
  - Volume: number of articles in the window
  - Recency: how recent the latest articles are
  - Velocity: rate of new articles (acceleration)

Score range: 0.0 (no trend) to 1.0 (hot trend).
"""

import math
from datetime import UTC, datetime, timedelta

from loguru import logger
from mapear_domain.models.base import SilverArticle


class TrendScorer:
    """Computes trend scores for entities and topics."""

    def __init__(
        self,
        window_hours: int = 48,
        decay_half_life_hours: float = 12.0,
    ) -> None:
        self.window = timedelta(hours=window_hours)
        self.decay_lambda = math.log(2) / (decay_half_life_hours * 3600)

    def _article_mentions_entity(
        self,
        entity_name: str,
        article: SilverArticle,
    ) -> bool:
        """Check if an article mentions the entity using structured fields first.

        Uses the pre-extracted mentioned_cities/mayors/governors/parties lists
        for accurate matching (respects aliases), falling back to text search.
        """
        entity_lower = entity_name.lower()

        # Check structured fields first (these already handle aliases)
        for field in (
            article.mentioned_cities,
            article.mentioned_mayors,
            article.mentioned_governors,
            article.mentioned_parties,
            getattr(article, "mentioned_persons", []),
        ):
            if any(e.lower() == entity_lower for e in field):
                return True

        # Fallback: text search for entities not in structured fields
        text_lower = f"{article.title} {article.content_clean}".lower()
        return entity_lower in text_lower

    def score_entity(
        self,
        entity_name: str,
        articles: list[SilverArticle],
        reference_time: datetime | None = None,
    ) -> float:
        """Compute trend score for an entity based on its mention frequency."""
        now = reference_time or datetime.now(UTC)
        cutoff = now - self.window

        relevant = []

        for article in articles:
            pub_time = article.published_at
            if pub_time is None:
                # Include articles without published_at (assume recent)
                pub_time = article.extracted_at
            if pub_time.tzinfo is None:
                pub_time = pub_time.replace(tzinfo=UTC)
            if pub_time < cutoff:
                continue

            if self._article_mentions_entity(entity_name, article):
                relevant.append(pub_time)

        if not relevant:
            return 0.0

        # Volume: scale to batch size (log-normalized)
        # For small batches, use a lower normalization base
        norm_base = max(len(articles), 10)
        volume = math.log1p(len(relevant)) / math.log1p(norm_base)
        volume = min(volume, 1.0)

        recency_scores = []
        for pub_time in relevant:
            age_seconds = max((now - pub_time).total_seconds(), 0)
            recency_scores.append(math.exp(-self.decay_lambda * age_seconds))
        recency = sum(recency_scores) / len(recency_scores)

        if len(relevant) >= 2:
            sorted_times = sorted(relevant)
            span = (sorted_times[-1] - sorted_times[0]).total_seconds()
            if span > 0:
                velocity = len(relevant) / (span / 3600)
                velocity = min(velocity / 5.0, 1.0)
            else:
                velocity = 0.5
        else:
            velocity = 0.1

        score = (volume * 0.35) + (recency * 0.40) + (velocity * 0.25)
        return round(min(score, 1.0), 4)

    def score_batch(
        self,
        entities: list[str],
        articles: list[SilverArticle],
    ) -> dict[str, float]:
        """Compute trend scores for multiple entities."""
        scores = {}
        for entity in entities:
            scores[entity] = self.score_entity(entity, articles)

        trending = {k: v for k, v in scores.items() if v > 0.3}
        if trending:
            logger.info(
                "Trending entities: {trending}",
                trending=trending,
            )

        return scores
