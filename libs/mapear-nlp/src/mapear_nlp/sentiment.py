"""Sentiment analysis with per-entity granularity.

Supports three modes via ENRICHMENT_MODE:
  - local:  Uses a lightweight transformer model (distilbert-based)
  - api:    Calls Google Cloud Natural Language API
  - skip:   Returns neutral sentiment (for testing pipeline flow)
"""

import re

from loguru import logger
from mapear_domain.models.base import SilverArticle
from mapear_infra.config import EnrichmentMode, get_settings


class SentimentAnalyzer:
    """Analyzes sentiment for articles and individual entities."""

    NEUTRAL = 0.0

    def __init__(self) -> None:
        settings = get_settings()
        self.mode = settings.enrichment_mode
        self.model_name = settings.sentiment_model
        self._pipeline = None
        self._gcp_client = None

    @property
    def pipeline(self):  # noqa: ANN201
        """Lazy-load the sentiment pipeline."""
        if self._pipeline is None and self.mode == EnrichmentMode.LOCAL:
            from transformers import pipeline as hf_pipeline

            logger.info(
                "Loading sentiment model: {model}",
                model=self.model_name,
            )
            self._pipeline = hf_pipeline(
                "sentiment-analysis",
                model=self.model_name,
                truncation=True,
                max_length=512,
            )
        return self._pipeline

    @property
    def gcp_client(self):  # noqa: ANN201
        """Lazy-load Google Cloud Natural Language client."""
        if self._gcp_client is None and self.mode == EnrichmentMode.API:
            from google.cloud import language_v2

            self._gcp_client = language_v2.LanguageServiceClient()
            logger.info("Initialized GCP Natural Language API client")
        return self._gcp_client

    def analyze_article(self, article: SilverArticle) -> dict:
        """Compute sentiment for an article."""
        if self.mode == EnrichmentMode.SKIP:
            return self._skip_result(article)

        if self.mode == EnrichmentMode.API:
            return self._analyze_article_gcp(article)

        overall = self._score_text(article.content_clean[:512])
        entity_sentiments = self._analyze_entities(article, overall_score=overall)

        return {
            "sentiment_overall": overall,
            "sentiment_by_entity": entity_sentiments,
        }

    def analyze_text(
        self,
        text: str,
        entities: list[tuple[str, str]] | None = None,
    ) -> dict:
        """Compute sentiment for a raw text string.

        Args:
            text: The text to analyze.
            entities: Optional list of (entity_name, entity_type) tuples for
                per-entity sentiment breakdown. If provided, sentences
                mentioning each entity are scored individually.
        """
        if self.mode == EnrichmentMode.SKIP:
            return {"sentiment_overall": self.NEUTRAL, "sentiment_by_entity": []}

        overall = self._score_text(text[:5000])

        entity_sentiments: list[dict] = []
        if entities:
            sentences = self._split_sentences(text)
            for entity_name, entity_type in entities:
                relevant = [s for s in sentences if entity_name.lower() in s.lower()]
                if relevant:
                    scores = [self._score_text(s) for s in relevant[:5]]
                    avg_score = sum(scores) / len(scores) if scores else self.NEUTRAL
                    entity_sentiments.append(
                        {
                            "entity": entity_name,
                            "entity_type": entity_type,
                            "sentiment": round(avg_score, 4),
                            "mention_count": len(relevant),
                            "sentiment_source": "entity",
                        }
                    )
                else:
                    # Entity is in the caller's list but no sentence matched the
                    # name — fall back to the document score so downstream
                    # consumers can still filter by sentiment_source.
                    entity_sentiments.append(
                        {
                            "entity": entity_name,
                            "entity_type": entity_type,
                            "sentiment": round(overall, 4),
                            "mention_count": 0,
                            "sentiment_source": "document",
                        }
                    )

        return {
            "sentiment_overall": overall,
            "sentiment_by_entity": entity_sentiments,
        }

    def analyze_texts(self, texts: list[str]) -> list[dict]:
        """Analyze sentiment for a batch of raw text strings."""
        results = []
        for text in texts:
            overall = self._score_text(text[:512])
            results.append({"score": overall})

        logger.info(
            "Sentiment text batch: {count} texts analyzed",
            count=len(results),
        )
        return results

    def analyze_batch(self, articles: list[SilverArticle]) -> list[dict]:
        """Analyze sentiment for a batch of articles."""
        results = []
        for article in articles:
            result = self.analyze_article(article)
            results.append(result)

        logger.info(
            "Sentiment batch: {count} articles analyzed",
            count=len(results),
        )
        return results

    # --- GCP Natural Language API ---

    def _analyze_article_gcp(self, article: SilverArticle) -> dict:
        """Analyze article sentiment via GCP Natural Language API.

        Overall score vem do ``analyze_sentiment`` no documento inteiro.
        O sentimento por entidade é calculado por sentenças que mencionam
        cada entidade RN (mesma estratégia do modo local), reaproveitando
        ``_analyze_entities`` — que já delega o scoring por sentença ao
        método ``_score_text``, o qual no modo API chama o GCP.

        A versão antiga chamava ``analyze_entities`` do GCP NL v2 e fazia
        substring-matching cego entre nomes de entidades retornadas e
        entidades RN, o que (a) marcava ``entity_type`` errado para
        siglas curtas como "RN" e (b) replicava o sentimento do
        documento como se fosse per-entidade — porque ``analyze_entities``
        do v2 não devolve sentiment per-entity. Removido em favor da
        rota baseada em sentenças, que produz scores reais.
        """
        text = article.content_clean[:5000]

        overall = self.NEUTRAL
        try:
            overall = self._score_text_gcp(text)
        except Exception as e:
            logger.warning("GCP sentiment analysis failed: {error}", error=str(e))

        entity_sentiments = self._analyze_entities(article, overall_score=overall)

        return {
            "sentiment_overall": overall,
            "sentiment_by_entity": entity_sentiments,
        }

    # --- Local model ---

    def _score_text(self, text: str) -> float:
        """Score a text snippet and return normalized sentiment (-1 to 1)."""
        if self.mode == EnrichmentMode.SKIP:
            return self.NEUTRAL

        if self.mode == EnrichmentMode.API:
            return self._score_text_gcp(text)

        if self.pipeline is None:
            return self.NEUTRAL

        try:
            result = self.pipeline(text[:512])[0]
            return self._normalize_score(result)
        except Exception as e:
            logger.warning("Sentiment scoring failed: {error}", error=str(e))
            return self.NEUTRAL

    def _score_text_gcp(self, text: str) -> float:
        """Score a single text via GCP Natural Language API."""
        from google.cloud import language_v2

        try:
            document = language_v2.Document(
                content=text[:5000],
                type_=language_v2.Document.Type.PLAIN_TEXT,
                language_code="pt",
            )
            response = self.gcp_client.analyze_sentiment(request={"document": document})
            return round(response.document_sentiment.score, 4)
        except Exception as e:
            logger.warning("GCP text sentiment failed: {error}", error=str(e))
            return self.NEUTRAL

    def _analyze_entities(
        self,
        article: SilverArticle,
        overall_score: float = 0.0,
    ) -> list[dict[str, float]]:
        """Compute sentiment for sentences mentioning each entity.

        Entities with matched sentences get ``sentiment_source="entity"`` and
        a score derived from those sentences. Entities in the article's mention
        lists that have no matching sentence get ``sentiment_source="document"``
        and fall back to the document-level score, so downstream consumers can
        filter by source without losing the entity record entirely.
        """
        entities_to_check: list[tuple[str, str]] = []

        for mayor in article.mentioned_mayors:
            entities_to_check.append((mayor, "mayor"))
        for governor in article.mentioned_governors:
            entities_to_check.append((governor, "governor"))
        for city in article.mentioned_cities:
            entities_to_check.append((city, "city"))
        for party in article.mentioned_parties:
            entities_to_check.append((party, "party"))

        if not entities_to_check:
            return []

        sentences = self._split_sentences(article.content_clean)
        results = []

        for entity_name, entity_type in entities_to_check:
            relevant = [s for s in sentences if entity_name.lower() in s.lower()]

            if relevant:
                scores = [self._score_text(s) for s in relevant[:5]]
                avg_score = sum(scores) / len(scores) if scores else self.NEUTRAL
                results.append(
                    {
                        "entity": entity_name,
                        "entity_type": entity_type,
                        "sentiment": round(avg_score, 4),
                        "mention_count": len(relevant),
                        "sentiment_source": "entity",
                    }
                )
            else:
                results.append(
                    {
                        "entity": entity_name,
                        "entity_type": entity_type,
                        "sentiment": round(overall_score, 4),
                        "mention_count": 0,
                        "sentiment_source": "document",
                    }
                )

        return results

    @staticmethod
    def _normalize_score(result: dict) -> float:
        """Normalize model output to -1..1 scale."""
        label = result.get("label", "")
        score = result.get("score", 0.5)

        star_match = re.search(r"(\d)", label)
        if star_match:
            stars = int(star_match.group(1))
            return round((stars - 3) / 2.0, 4)

        label_lower = label.lower()
        if "positive" in label_lower:
            return round(score, 4)
        if "negative" in label_lower:
            return round(-score, 4)

        return 0.0

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split text into sentences (simple regex approach)."""
        sentences = re.split(r"(?<=[.!?])\s+", text)
        return [s.strip() for s in sentences if len(s.strip()) > 20]

    def _skip_result(self, article: SilverArticle) -> dict:
        """Return neutral results when enrichment is skipped."""
        return {
            "sentiment_overall": self.NEUTRAL,
            "sentiment_by_entity": [],
        }
