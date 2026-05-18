"""Unit tests for topic_modeling producer changes (TDT-TOPIC-01 E2).

Validates that classify_by_keywords and _classify_text_gcp emit topic_id_source
in every code path, and that the fallback chain in fit_transform propagates
the correct source discriminator.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from mapear_nlp.topic_modeling import (
    EnrichmentMode,
    TopicModeler,
    classify_by_keywords,
)

# google.cloud.language_v2 is a prod-only dependency; mock it for unit tests.
_GOOGLE_MOCK = MagicMock()
_GOOGLE_CLOUD_MOCK = MagicMock()
_LANGUAGE_V2_MOCK = MagicMock()
_GOOGLE_CLOUD_MOCK.language_v2 = _LANGUAGE_V2_MOCK
_GOOGLE_MOCK.cloud = _GOOGLE_CLOUD_MOCK

_GCP_MODULES = {
    "google": _GOOGLE_MOCK,
    "google.cloud": _GOOGLE_CLOUD_MOCK,
    "google.cloud.language_v2": _LANGUAGE_V2_MOCK,
}


class TestClassifyByKeywords:
    def test_match_returns_keyword_map_source(self):
        result = classify_by_keywords("eleições 2026 candidatos voto campanha")
        assert result["topic_id"] != -1
        assert result["topic_id_source"] == "keyword_map"

    def test_no_match_returns_unclassified_source(self):
        result = classify_by_keywords("lorem ipsum dolor sit amet")
        assert result["topic_id"] == -1
        assert result["topic_id_source"] == "unclassified"

    def test_topic_id_source_always_present(self):
        for text in ["", "a", "eleições", "xyz abc def"]:
            result = classify_by_keywords(text)
            assert "topic_id_source" in result

    def test_match_returns_topic_label_raw_in_topic_id_map(self):
        from mapear_nlp.topic_modeling import TOPIC_ID_MAP

        result = classify_by_keywords("eleições 2026 candidatos voto campanha")
        assert result["topic_id_source"] == "keyword_map"
        assert isinstance(result["topic_label_raw"], str)
        assert result["topic_label_raw"] in TOPIC_ID_MAP

    def test_no_match_returns_topic_label_raw_none(self):
        result = classify_by_keywords("lorem ipsum dolor sit amet")
        assert result["topic_id_source"] == "unclassified"
        assert result["topic_label_raw"] is None

    def test_topic_label_raw_always_present(self):
        for text in ["", "a", "eleições", "xyz abc def"]:
            result = classify_by_keywords(text)
            assert "topic_label_raw" in result


def _make_modeler(mode: EnrichmentMode = EnrichmentMode.SKIP) -> TopicModeler:
    modeler = object.__new__(TopicModeler)
    modeler.mode = mode
    modeler._model = None
    modeler._gcp_client = MagicMock()
    modeler._model_path = MagicMock()
    modeler._fitted = False
    return modeler


class TestClassifyTextGcp:
    def test_gcp_success_returns_gcp_ordinal_source(self):
        modeler = _make_modeler(EnrichmentMode.API)
        mock_category = MagicMock()
        mock_category.name = "/Law & Government/Government"
        mock_category.confidence = 0.9
        modeler._gcp_client.classify_text.return_value = MagicMock(
            categories=[mock_category]
        )
        with patch.dict(sys.modules, _GCP_MODULES):
            result = modeler._classify_text_gcp(
                "governo estadual administração pública"
            )
        assert result["topic_id"] != -1
        assert result["topic_id_source"] == "gcp_ordinal"

    def test_gcp_success_returns_topic_label_raw(self):
        modeler = _make_modeler(EnrichmentMode.API)
        mock_category = MagicMock()
        mock_category.name = "/Law & Government/Government"
        mock_category.confidence = 0.9
        modeler._gcp_client.classify_text.return_value = MagicMock(
            categories=[mock_category]
        )
        with patch.dict(sys.modules, _GCP_MODULES):
            result = modeler._classify_text_gcp(
                "governo estadual administração pública"
            )
        assert result["topic_id_source"] == "gcp_ordinal"
        assert isinstance(result["topic_label_raw"], str)
        assert len(result["topic_label_raw"]) > 0

    def test_gcp_failure_returns_unclassified_source(self):
        modeler = _make_modeler(EnrichmentMode.API)
        modeler._gcp_client.classify_text.side_effect = Exception("API error")
        with patch.dict(sys.modules, _GCP_MODULES):
            result = modeler._classify_text_gcp("some text long enough to pass here")
        assert result["topic_id"] == -1
        assert result["topic_id_source"] == "unclassified"

    def test_gcp_failure_returns_topic_label_raw_none(self):
        modeler = _make_modeler(EnrichmentMode.API)
        modeler._gcp_client.classify_text.side_effect = Exception("API error")
        with patch.dict(sys.modules, _GCP_MODULES):
            result = modeler._classify_text_gcp("some text long enough to pass here")
        assert result["topic_id_source"] == "unclassified"
        assert result["topic_label_raw"] is None

    def test_short_text_returns_unclassified_source(self):
        modeler = _make_modeler(EnrichmentMode.API)
        with patch.dict(sys.modules, _GCP_MODULES):
            result = modeler._classify_text_gcp("curto")
        assert result["topic_id"] == -1
        assert result["topic_id_source"] == "unclassified"

    def test_short_text_returns_topic_label_raw_none(self):
        modeler = _make_modeler(EnrichmentMode.API)
        with patch.dict(sys.modules, _GCP_MODULES):
            result = modeler._classify_text_gcp("curto")
        assert result["topic_label_raw"] is None

    def test_gcp_empty_categories_returns_unclassified_source(self):
        modeler = _make_modeler(EnrichmentMode.API)
        modeler._gcp_client.classify_text.return_value = MagicMock(categories=[])
        with patch.dict(sys.modules, _GCP_MODULES):
            result = modeler._classify_text_gcp("texto suficientemente longo aqui")
        assert result["topic_id"] == -1
        assert result["topic_id_source"] == "unclassified"

    def test_gcp_empty_categories_returns_topic_label_raw_none(self):
        modeler = _make_modeler(EnrichmentMode.API)
        modeler._gcp_client.classify_text.return_value = MagicMock(categories=[])
        with patch.dict(sys.modules, _GCP_MODULES):
            result = modeler._classify_text_gcp("texto suficientemente longo aqui")
        assert result["topic_label_raw"] is None


def _make_article(text: str = "eleições 2026 campanha candidatos") -> MagicMock:
    article = MagicMock()
    article.title = ""
    article.content_clean = text
    return article


class TestFitTransformSourcePropagation:
    """Integration-level tests for topic_id_source propagation in fit_transform."""

    def test_skip_mode_keyword_success_emits_keyword_map(self):
        modeler = _make_modeler(EnrichmentMode.SKIP)
        results = modeler.fit_transform(
            [_make_article("eleições 2026 candidatos voto")]
        )
        assert results[0]["topic_id_source"] == "keyword_map"

    def test_skip_mode_no_match_emits_unclassified(self):
        modeler = _make_modeler(EnrichmentMode.SKIP)
        results = modeler.fit_transform([_make_article("lorem ipsum dolor")])
        assert results[0]["topic_id"] == -1
        assert results[0]["topic_id_source"] == "unclassified"

    def test_api_mode_gcp_success_emits_gcp_ordinal(self):
        modeler = _make_modeler(EnrichmentMode.API)
        gcp_result = {
            "topic_id": 0,
            "topics": ["governo"],
            "topic_id_source": "gcp_ordinal",
        }
        with patch.object(modeler, "_classify_batch_gcp", return_value=[gcp_result]):
            results = modeler.fit_transform([_make_article()])
        assert results[0]["topic_id_source"] == "gcp_ordinal"

    def test_api_mode_gcp_fail_keyword_success_emits_keyword_map(self):
        modeler = _make_modeler(EnrichmentMode.API)
        gcp_result = {"topic_id": -1, "topics": [], "topic_id_source": "unclassified"}
        with patch.object(modeler, "_classify_batch_gcp", return_value=[gcp_result]):
            results = modeler.fit_transform(
                [_make_article("eleições 2026 candidatos voto")]
            )
        assert results[0]["topic_id_source"] == "keyword_map"
        assert results[0]["topic_id"] != -1

    def test_api_mode_both_fail_emits_unclassified(self):
        modeler = _make_modeler(EnrichmentMode.API)
        gcp_result = {"topic_id": -1, "topics": [], "topic_id_source": "unclassified"}
        with patch.object(modeler, "_classify_batch_gcp", return_value=[gcp_result]):
            results = modeler.fit_transform([_make_article("lorem ipsum dolor")])
        assert results[0]["topic_id"] == -1
        assert results[0]["topic_id_source"] == "unclassified"
