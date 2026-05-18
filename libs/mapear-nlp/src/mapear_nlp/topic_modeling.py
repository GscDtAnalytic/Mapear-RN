"""Topic modeling using BERTopic (local) or GCP classify_text (api).

Generates topic clusters from content, filtered by RN relevance.
Target themes: saúde pública, educação, segurança, infraestrutura,
meio ambiente, orçamento municipal, escândalos políticos.
"""

from pathlib import Path

from loguru import logger
from mapear_domain.models.base import SilverArticle
from mapear_infra.config import EnrichmentMode, get_settings

SEED_TOPICS = [
    ["saúde", "hospital", "UBS", "SUS", "médico", "vacina", "pandemia"],
    ["educação", "escola", "professor", "aluno", "creche", "universidade", "ENEM"],
    ["segurança", "polícia", "crime", "homicídio", "violência", "assalto", "tráfico"],
    [
        "infraestrutura",
        "obra",
        "pavimentação",
        "saneamento",
        "estrada",
        "ponte",
        "transporte",
    ],
    ["meio ambiente", "desmatamento", "poluição", "água", "seca", "clima", "lixo"],
    ["orçamento", "licitação", "imposto", "IPTU", "receita", "dívida", "verba"],
    ["escândalo", "corrupção", "denúncia", "investigação", "CPI", "fraude", "desvio"],
]

# Maps GCP content categories to our political monitoring topics.
# GCP classify_text returns categories like "/News/Politics",
# "/Law & Government", "/Health", etc.
GCP_CATEGORY_MAP = {
    "Health": ["saúde", "hospital", "SUS"],
    "Education": ["educação", "escola", "professor"],
    "Law & Government": ["governo", "legislação", "gestão pública"],
    "Crime": ["policial", "segurança", "crime"],
    "Politics": ["política", "eleição", "campanha"],
    "Finance": ["orçamento", "imposto", "licitação"],
    "Environment": ["meio ambiente", "saneamento", "clima"],
    "Infrastructure": ["infraestrutura", "obra", "transporte"],
    "Social Services": ["assistência social", "programa social"],
}

# Keyword-based topic classifier for disambiguation and fallback.
# Used when GCP/BERTopic returns ambiguous results (e.g. "polícia" ≠ "política").
# Each topic has a stable numeric ID for the topic_id field.
PROJECT_TOPICS: dict[str, list[str]] = {
    "eleições_2026": [
        "candidat",
        "eleição",
        "eleicao",
        "pré-candidato",
        "campanha",
        "palanque",
        "chapa",
        "urna",
        "voto",
    ],
    "governo_estadual": [
        "governador",
        "governadora",
        "governo do rn",
        "assembleia legislativa",
        "palácio",
    ],
    "gestão_municipal": [
        "prefeito",
        "prefeita",
        "prefeitura",
        "câmara municipal",
        "vereador",
    ],
    "políticas_públicas": [
        "programa social",
        "saúde pública",
        "educação",
        "saneamento",
        "infraestrutura",
    ],
    "segurança": [
        "segurança pública",
        "criminalidade",
        "homicídio",
    ],
    "economia_local": [
        "emprego",
        "indústria",
        "comércio",
        "agronegócio",
        "petróleo",
    ],
    "policial": [
        "preso",
        "acidente",
        "blitz",
        "furto",
        "operação policial",
        "delegacia",
        "assalto",
        "roubo",
        "apreensão",
    ],
    "saúde": [
        "hospital",
        "ubs",
        "sus",
        "médico",
        "vacina",
        "pandemia",
        "saúde",
        "enfermeiro",
        "leito",
    ],
    "educação": [
        "escola",
        "professor",
        "aluno",
        "creche",
        "universidade",
        "enem",
        "ensino",
    ],
    "meio_ambiente": [
        "desmatamento",
        "poluição",
        "água",
        "seca",
        "clima",
        "lixo",
        "ambiental",
    ],
}

# Stable topic_id mapping — never change existing IDs, only append new ones.
TOPIC_ID_MAP: dict[str, int] = {
    "eleições_2026": 1,
    "governo_estadual": 2,
    "gestão_municipal": 3,
    "políticas_públicas": 4,
    "segurança": 5,
    "economia_local": 6,
    "policial": 7,
    "saúde": 8,
    "educação": 9,
    "meio_ambiente": 10,
}

# Terms that indicate political context (to disambiguate "polícia" vs "política")
_POLITICAL_TERMS = {
    "eleição",
    "eleicao",
    "governo",
    "prefeito",
    "prefeita",
    "vereador",
    "candidato",
    "candidata",
    "deputado",
    "senador",
    "governador",
    "governadora",
    "partido",
    "mandato",
    "campanha",
}


def classify_by_keywords(text: str) -> dict:
    """Classify text using keyword matching as fallback/disambiguation.

    Returns topic with highest keyword match count. Uses stable topic IDs
    from TOPIC_ID_MAP to ensure granularity (10 distinct topics).
    """
    text_lower = text.lower()

    # Disambiguation: if "polícia" present but no political terms,
    # prefer "policial" over "política"
    has_policia = "polícia" in text_lower or "policia" in text_lower
    has_political = any(term in text_lower for term in _POLITICAL_TERMS)

    best_topic = ""
    best_count = 0

    for topic, keywords in PROJECT_TOPICS.items():
        count = sum(1 for kw in keywords if kw.lower() in text_lower)
        if count > best_count:
            best_count = count
            best_topic = topic

    # Override: if classified as political but only has police content
    if (
        has_policia
        and not has_political
        and best_topic not in ("policial", "segurança")
    ):
        best_topic = "policial"
        best_count = 1

    if best_count > 0:
        return {
            "topic_id": TOPIC_ID_MAP.get(best_topic, 0),
            "topics": PROJECT_TOPICS.get(best_topic, [])[:5],
            "topic_label": best_topic,
            "topic_id_source": "keyword_map",
            "topic_label_raw": best_topic,
        }

    return {
        "topic_id": -1,
        "topics": [],
        "topic_label": "",
        "topic_id_source": "unclassified",
        "topic_label_raw": None,
    }


MODEL_DIR = Path("data/models")
MODEL_PATH = MODEL_DIR / "bertopic_rn"


class TopicModeler:
    """Clusters articles into thematic topics."""

    def __init__(self, model_path: Path | None = None) -> None:
        settings = get_settings()
        self.mode = settings.enrichment_mode
        self._model = None
        self._gcp_client = None
        self._model_path = model_path or MODEL_PATH
        self._fitted = False

    @property
    def model(self):  # noqa: ANN201
        """Lazy-load BERTopic model — from disk if available, else new."""
        if self._model is None and self.mode == EnrichmentMode.LOCAL:
            self._model = self._load_or_create()
        return self._model

    @property
    def gcp_client(self):  # noqa: ANN201
        """Lazy-load Google Cloud Natural Language client."""
        if self._gcp_client is None and self.mode == EnrichmentMode.API:
            from google.cloud import language_v2

            self._gcp_client = language_v2.LanguageServiceClient()
            logger.info("Initialized GCP Natural Language API client for topics")
        return self._gcp_client

    def _load_or_create(self):  # noqa: ANN201
        """Load persisted model or create a fresh one."""
        from bertopic import BERTopic
        from sentence_transformers import SentenceTransformer

        embedding_model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

        if self._model_path.exists():
            logger.info(
                "Loading persisted BERTopic model from {path}",
                path=str(self._model_path),
            )
            model = BERTopic.load(
                str(self._model_path),
                embedding_model=embedding_model,
            )
            self._fitted = True
            return model

        logger.info("Creating new BERTopic model (no persisted model found)")
        return BERTopic(
            embedding_model=embedding_model,
            language="multilingual",
            seed_topic_list=SEED_TOPICS,
            min_topic_size=3,
            nr_topics="auto",
            verbose=False,
        )

    def _save_model(self) -> None:
        """Persist the fitted model to disk."""
        if self._model is None:
            return
        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        self._model.save(
            str(self._model_path),
            serialization="safetensors",
            save_ctfidf=True,
            save_embedding_model="paraphrase-multilingual-MiniLM-L12-v2",
        )
        logger.info(
            "BERTopic model saved to {path}",
            path=str(self._model_path),
        )

    def _classify_text_gcp(self, text: str) -> dict:
        """Classify a single text using GCP Natural Language API."""
        from google.cloud import language_v2

        if len(text.strip()) < 20:
            return {
                "topic_id": -1,
                "topics": [],
                "topic_id_source": "unclassified",
                "topic_label_raw": None,
            }

        try:
            document = language_v2.Document(
                content=text[:5000],
                type_=language_v2.Document.Type.PLAIN_TEXT,
                language_code="pt",
            )
            response = self.gcp_client.classify_text(request={"document": document})

            topics: list[str] = []
            best_confidence = 0.0
            topic_id = -1
            best_category_name: str | None = None

            for idx, category in enumerate(response.categories):
                cat_parts = category.name.strip("/").split("/")
                for part in cat_parts:
                    mapped = GCP_CATEGORY_MAP.get(part, [])
                    topics.extend(mapped)
                if category.confidence > best_confidence:
                    best_confidence = category.confidence
                    topic_id = idx
                    best_category_name = category.name

            seen: set[str] = set()
            unique_topics = []
            for t in topics:
                if t not in seen:
                    seen.add(t)
                    unique_topics.append(t)

            resolved_id = topic_id if unique_topics else -1
            return {
                "topic_id": resolved_id,
                "topics": unique_topics[:5],
                "topic_id_source": (
                    "gcp_ordinal" if resolved_id != -1 else "unclassified"
                ),
                "topic_label_raw": best_category_name if resolved_id != -1 else None,
            }
        except Exception as e:
            logger.warning("GCP classify_text failed: {error}", error=str(e))
            return {
                "topic_id": -1,
                "topics": [],
                "topic_id_source": "unclassified",
                "topic_label_raw": None,
            }

    def _keyword_fallback(self, articles: list[SilverArticle]) -> list[dict]:
        """Classify articles using keyword matching (always available)."""
        results = []
        for article in articles:
            text = f"{article.title} {article.content_clean}"
            results.append(classify_by_keywords(text))
        return results

    def fit_transform(self, articles: list[SilverArticle]) -> list[dict]:
        """Fit or transform topics on a batch.

        Uses keyword-based classification as the primary method for small
        batches or when BERTopic/GCP returns -1 (unclassified).
        """
        if not articles:
            return []

        if self.mode == EnrichmentMode.SKIP:
            return self._keyword_fallback(articles)

        if self.mode == EnrichmentMode.API:
            results = self._classify_batch_gcp(articles)
            # Fall back to keywords for unclassified articles
            for i, (result, article) in enumerate(zip(results, articles, strict=False)):
                if result["topic_id"] == -1:
                    text = f"{article.title} {article.content_clean}"
                    results[i] = classify_by_keywords(text)
            return results

        if self.model is None:
            return self._keyword_fallback(articles)

        try:
            docs = [a.content_clean for a in articles]

            if self._fitted:
                topics, _ = self.model.transform(docs)
            else:
                topics, _ = self.model.fit_transform(docs)
                self._fitted = True
                self._save_model()

            topic_info = self.model.get_topic_info()
            topic_labels = {}
            for _, row in topic_info.iterrows():
                tid = row["Topic"]
                if tid != -1:
                    topic_words = self.model.get_topic(tid)
                    topic_labels[tid] = [w for w, _ in topic_words[:5]]

            results = []
            for idx, topic_id in enumerate(topics):
                if topic_id == -1:
                    # BERTopic couldn't classify — use keyword fallback
                    text = f"{articles[idx].title} {articles[idx].content_clean}"
                    results.append(classify_by_keywords(text))
                else:
                    results.append(
                        {
                            "topic_id": int(topic_id),
                            "topics": topic_labels.get(topic_id, []),
                            "topic_id_source": None,
                            "topic_label_raw": None,
                        }
                    )

            unique_topics = {r["topic_id"] for r in results if r["topic_id"] != -1}
            logger.info(
                "Topic modeling: {docs} docs, {topics} distinct topics",
                docs=len(docs),
                topics=len(unique_topics),
            )

            return results

        except Exception as e:
            logger.error(
                "Topic modeling failed, using keyword fallback: {error}", error=str(e)
            )
            return self._keyword_fallback(articles)

    def _classify_batch_gcp(self, articles: list[SilverArticle]) -> list[dict]:
        """Classify a batch of articles via GCP Natural Language API."""
        results = []
        topic_count = 0

        for article in articles:
            result = self._classify_text_gcp(article.content_clean)
            results.append(result)
            if result["topic_id"] != -1:
                topic_count += 1

        logger.info(
            "GCP classify_text: {docs} docs, {classified} classified",
            docs=len(articles),
            classified=topic_count,
        )
        return results

    def transform_single(self, text: str) -> dict:
        """Assign topic to a single document (after fit)."""
        if self.mode == EnrichmentMode.SKIP:
            return {
                "topic_id": -1,
                "topics": [],
                "topic_id_source": "unclassified",
                "topic_label_raw": None,
            }

        if self.mode == EnrichmentMode.API:
            return self._classify_text_gcp(text)

        if self.model is None:
            return {
                "topic_id": -1,
                "topics": [],
                "topic_id_source": "unclassified",
                "topic_label_raw": None,
            }

        try:
            topics, _ = self.model.transform([text])
            topic_id = int(topics[0])
            topic_words = self.model.get_topic(topic_id) if topic_id != -1 else []
            return {
                "topic_id": topic_id,
                "topics": [w for w, _ in topic_words[:5]],
                "topic_id_source": None,
                "topic_label_raw": None,
            }
        except Exception:
            return {
                "topic_id": -1,
                "topics": [],
                "topic_id_source": "unclassified",
                "topic_label_raw": None,
            }
