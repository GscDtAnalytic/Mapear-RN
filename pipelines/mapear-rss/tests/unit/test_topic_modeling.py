"""Tests for topic modeling and keyword classifier."""

import pytest

from mapear_domain.models.base import SilverArticle
from mapear_nlp.topic_modeling import (
    TOPIC_ID_MAP,
    TopicModeler,
    classify_by_keywords,
)


@pytest.fixture
def modeler(monkeypatch: pytest.MonkeyPatch) -> TopicModeler:
    monkeypatch.setenv("ENRICHMENT_MODE", "skip")
    return TopicModeler()


@pytest.fixture
def silver_articles() -> list[SilverArticle]:
    base = {
        "source_feed": "test",
        "extracted_at": "2026-04-03T12:00:00",
        "is_rn_relevant": True,
    }
    return [
        SilverArticle(
            url="https://example.com/1",
            title="Saúde em Natal recebe investimentos",
            content_clean="Hospital municipal terá mais 50 leitos de UTI.",
            content_hash="hash1",
            **base,
        ),
        SilverArticle(
            url="https://example.com/2",
            title="Nova escola inaugurada em Mossoró",
            content_clean="Prefeito inaugurou escola com capacidade para 500 alunos.",
            content_hash="hash2",
            **base,
        ),
    ]


class TestClassifyByKeywords:
    def test_policial_not_politica(self):
        """Articles about police should NOT be classified as political."""
        text = "Polícia prende suspeito de furto em delegacia após blitz"
        result = classify_by_keywords(text)
        assert result["topic_label"] == "policial"
        assert result["topic_id"] == TOPIC_ID_MAP["policial"]

    def test_political_with_political_terms(self):
        """Articles with political terms should get political topics."""
        text = "Candidato a governador anuncia campanha eleitoral para eleição 2026"
        result = classify_by_keywords(text)
        assert result["topic_label"] == "eleições_2026"
        assert result["topic_id"] == TOPIC_ID_MAP["eleições_2026"]

    def test_gestao_municipal(self):
        text = "Prefeito e vereadores debatem projeto na câmara municipal"
        result = classify_by_keywords(text)
        assert result["topic_label"] == "gestão_municipal"
        assert result["topic_id"] == TOPIC_ID_MAP["gestão_municipal"]

    def test_saude_topic(self):
        text = "Hospital do SUS amplia vacinação com mais médicos no UBS"
        result = classify_by_keywords(text)
        assert result["topic_label"] == "saúde"
        assert result["topic_id"] == TOPIC_ID_MAP["saúde"]

    def test_educacao_topic(self):
        text = "Escola recebe novos professores para ensino de alunos"
        result = classify_by_keywords(text)
        assert result["topic_label"] == "educação"
        assert result["topic_id"] == TOPIC_ID_MAP["educação"]

    def test_no_match_returns_minus_one(self):
        text = "Lorem ipsum dolor sit amet consectetur adipiscing elit"
        result = classify_by_keywords(text)
        assert result["topic_id"] == -1
        assert result["topic_label"] == ""

    def test_policia_disambiguation_override(self):
        """'polícia' without political terms forces policial topic."""
        text = "Acidente de trânsito na frente da delegacia de polícia"
        result = classify_by_keywords(text)
        assert result["topic_label"] == "policial"

    def test_all_topic_ids_are_unique(self):
        ids = list(TOPIC_ID_MAP.values())
        assert len(ids) == len(set(ids)), "Topic IDs must be unique"

    def test_all_topic_ids_positive(self):
        for label, tid in TOPIC_ID_MAP.items():
            assert tid > 0, f"{label} has non-positive topic_id={tid}"


class TestTopicModeler:
    def test_skip_mode_uses_keyword_fallback(
        self, modeler: TopicModeler, silver_articles: list[SilverArticle]
    ) -> None:
        results = modeler.fit_transform(silver_articles)
        assert len(results) == 2
        # "Hospital" + "UTI" → saúde topic
        assert results[0]["topic_id"] == TOPIC_ID_MAP["saúde"]
        # "Prefeito" + "escola" → gestão_municipal or educação
        assert results[1]["topic_id"] in (
            TOPIC_ID_MAP["gestão_municipal"],
            TOPIC_ID_MAP["educação"],
        )

    def test_empty_input(self, modeler: TopicModeler) -> None:
        results = modeler.fit_transform([])
        assert results == []

    def test_transform_single_skip(self, modeler: TopicModeler) -> None:
        result = modeler.transform_single("Texto qualquer sobre política")
        assert result["topic_id"] == -1
