"""Unit tests for TDT-TOPIC-01 backfill marking logic (TDT-TOPIC-01 E4)."""

from __future__ import annotations

import pytest


def _resolve(topic_id_stored: int, keyword_topic_id: int) -> str:
    """Isolated deterministic marking rule — mirrors backfill script logic.

    keyword_topic_id must be in 1-10 (valid TOPIC_ID_MAP range) to be considered
    a keyword_map match. 0 is a GCP ordinal index, never emitted by keyword classifier.
    """
    if keyword_topic_id == topic_id_stored and 1 <= keyword_topic_id <= 10:
        return "keyword_map"
    return "gcp_ordinal"


class TestMarkingRule:
    def test_keyword_agrees_with_stored_returns_keyword_map(self):
        assert _resolve(topic_id_stored=3, keyword_topic_id=3) == "keyword_map"

    def test_keyword_disagrees_returns_gcp_ordinal(self):
        assert _resolve(topic_id_stored=0, keyword_topic_id=3) == "gcp_ordinal"

    def test_keyword_fails_stored_positive_returns_gcp_ordinal(self):
        assert _resolve(topic_id_stored=2, keyword_topic_id=-1) == "gcp_ordinal"

    def test_both_negative_one_returns_gcp_ordinal(self):
        # topic_id=-1 rows are handled upstream as 'unclassified'; this case
        # should not appear in the reclassification pass.
        assert _resolve(topic_id_stored=-1, keyword_topic_id=-1) == "gcp_ordinal"

    def test_keyword_zero_does_not_match_stored_zero(self):
        # topic_id=0 from GCP is ordinal; keyword returning 0 would mean
        # TOPIC_ID_MAP.get(best_topic, 0) fallback — treat as disagree.
        assert _resolve(topic_id_stored=0, keyword_topic_id=0) == "gcp_ordinal"

    @pytest.mark.parametrize("tid", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    def test_all_valid_keyword_map_ids_resolve_correctly(self, tid: int):
        assert _resolve(topic_id_stored=tid, keyword_topic_id=tid) == "keyword_map"

    @pytest.mark.parametrize("stored,kw", [(1, 2), (3, 5), (7, 10), (2, 0)])
    def test_mismatched_ids_always_gcp_ordinal(self, stored: int, kw: int):
        assert _resolve(topic_id_stored=stored, keyword_topic_id=kw) == "gcp_ordinal"
