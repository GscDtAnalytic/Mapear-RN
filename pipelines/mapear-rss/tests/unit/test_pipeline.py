"""Tests for the pipeline helpers.

Focus: the BQ load error surface that caused the 2026-04-18 incident where
Cloud Run marked runs as succeeded=1 despite silver/gold loads failing.
Includes checkpoint save/load and CLI argument parsing tests.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pandas as pd
import pyarrow as pa

from mapear_domain.models.base import SilverArticle
from mapear_rss.pipeline import (
    _load_checkpoint,
    _load_to_warehouse,
    _parse_args,
    _save_checkpoint,
    _silver_to_gold,
    _write_silver_to_iceberg,
)


def test_load_to_warehouse_success_does_not_touch_failed_loads():
    warehouse = MagicMock()
    warehouse.load.return_value = 42
    failed_loads: list[str] = []

    _load_to_warehouse(
        warehouse,
        "gs://bucket/raw/data.parquet",
        "project.dataset.table",
        failed_loads,
    )

    assert failed_loads == []
    warehouse.load.assert_called_once_with(
        "gs://bucket/raw/data.parquet",
        "project.dataset.table",
        merge_key=None,
    )


def test_load_to_warehouse_records_failed_table_on_exception():
    warehouse = MagicMock()
    warehouse.load.side_effect = RuntimeError("schema mismatch")
    failed_loads: list[str] = []

    _load_to_warehouse(
        warehouse,
        "gs://bucket/silver/data.parquet",
        "project.mapear_silver.silver_articles",
        failed_loads,
    )

    assert failed_loads == ["project.mapear_silver.silver_articles"]


def test_load_to_warehouse_noops_when_warehouse_is_none():
    failed_loads: list[str] = []

    _load_to_warehouse(None, "gs://bucket/data.parquet", "table", failed_loads)

    assert failed_loads == []


# ---------------------------------------------------------------------------
# Checkpoint tests
# ---------------------------------------------------------------------------


def test_save_and_load_checkpoint(tmp_path, monkeypatch):
    checkpoint_file = tmp_path / "checkpoint.json"
    monkeypatch.setattr("mapear_rss.pipeline._CHECKPOINT_PATH", checkpoint_file)

    urls = {"https://example.com/a", "https://example.com/b"}
    _save_checkpoint(urls)

    loaded = _load_checkpoint()
    assert loaded == urls


def test_load_checkpoint_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mapear_rss.pipeline._CHECKPOINT_PATH",
        tmp_path / "nonexistent.json",
    )
    assert _load_checkpoint() == set()


def test_load_checkpoint_corrupted_file(tmp_path, monkeypatch):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not valid json {{{")
    monkeypatch.setattr("mapear_rss.pipeline._CHECKPOINT_PATH", bad_file)
    assert _load_checkpoint() == set()


def test_checkpoint_resume_excludes_already_processed(tmp_path, monkeypatch):
    checkpoint_file = tmp_path / "checkpoint.json"
    monkeypatch.setattr("mapear_rss.pipeline._CHECKPOINT_PATH", checkpoint_file)

    already = {"https://example.com/old-article"}
    _save_checkpoint(already)
    loaded = _load_checkpoint()
    assert "https://example.com/old-article" in loaded
    assert "https://example.com/new-article" not in loaded


# ---------------------------------------------------------------------------
# CLI argument parser tests
# ---------------------------------------------------------------------------


def test_parse_args_defaults():
    args = _parse_args([])
    assert args.backfill_since is None
    assert args.backfill_start_date is None
    assert args.batch_size == 10
    assert args.checkpoint_interval == 50


def test_parse_args_backfill_start_date():
    args = _parse_args(["--backfill-start-date", "2025-01-01"])
    assert args.backfill_start_date == "2025-01-01"


def test_parse_args_backfill_since():
    args = _parse_args(["--backfill-since", "2025-01-01T00:00:00Z"])
    assert args.backfill_since == "2025-01-01T00:00:00Z"


def test_parse_args_batch_and_checkpoint():
    args = _parse_args(["--batch-size", "20", "--checkpoint-interval", "100"])
    assert args.batch_size == 20
    assert args.checkpoint_interval == 100


# --- Silver→Gold bridge (TDT-RSS-PERSON-01 regression guard) ----------------


def _silver_with_person(person_id: str | None, scope: str | None) -> SilverArticle:
    return SilverArticle(
        url="https://example.com/article-1",
        source_feed="https://example.com/feed",
        title="Prefeito x anuncia obra",
        content_clean="Texto limpo",
        author=None,
        published_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        extracted_at=datetime(2026, 5, 1, 12, 5, tzinfo=UTC),
        content_hash="abc123",
        is_rn_relevant=True,
        mentioned_cities=["Natal"],
        mentioned_mayors=["Prefeito X"],
        mentioned_governors=[],
        mentioned_parties=[],
        mentioned_persons=["Prefeito X"],
        person_id=person_id,
        scope_status=scope,  # type: ignore[arg-type]
        ingestion_run_id="rss-test1234",
        pipeline_version="0.1.0-test",
    )


def _stub_sent_topic() -> tuple[dict, dict]:
    sent = {"sentiment_overall": 0.0, "sentiment_by_entity": []}
    topic = {
        "topics": [],
        "topic_id": 1,
        "topic_label": "saude",
        "topic_id_source": "keyword_map",
        "topic_label_raw": "saúde",
    }
    return sent, topic


def test_silver_to_gold_propagates_person_id_and_scope_status():
    """Regressão TDT-RSS-PERSON-01: PersonResolver popula person_id em
    SilverArticle, mas a construção do GoldArticle precisa propagar o
    overlay eleitoral senão fct_content_gold filtra 100% do RSS."""
    silver = _silver_with_person("mayor_paulinho_freire", "IN_SCOPE")
    sent, topic = _stub_sent_topic()

    gold = _silver_to_gold(article=silver, sent=sent, topic=topic, trend_score=0.5)

    assert gold.person_id == "mayor_paulinho_freire"
    assert gold.scope_status == "IN_SCOPE"
    assert gold.author_in_scope is True


def test_silver_to_gold_passes_through_null_person_id():
    """Quando PersonResolver não resolve (OUT_OF_SCOPE), o gold preserva
    NULL — é o sinal que o gate downstream usa para excluir o evento."""
    silver = _silver_with_person(None, "OUT_OF_SCOPE")
    sent, topic = _stub_sent_topic()

    gold = _silver_to_gold(article=silver, sent=sent, topic=topic, trend_score=0.0)

    assert gold.person_id is None
    assert gold.scope_status == "OUT_OF_SCOPE"
    assert gold.author_in_scope is False


def test_silver_to_gold_propagates_topic_label_raw():
    """Regressão TDT-TOPIC-01: mapear-nlp produz topic_label_raw e a
    construção do GoldArticle precisa propagá-lo, senão o invariante
    assert_topic_id_source_invariants quebra (NULL para keyword_map/gcp_ordinal)."""
    silver = _silver_with_person("mayor_x", "IN_SCOPE")
    sent, topic = _stub_sent_topic()

    gold = _silver_to_gold(article=silver, sent=sent, topic=topic, trend_score=0.0)

    assert gold.topic_label_raw == "saúde"
    assert gold.topic_id_source == "keyword_map"


def test_silver_to_gold_propagates_lineage():
    """Regressão TDT-RSS-LINEAGE: ingestion_run_id e pipeline_version
    populados no silver loop devem chegar ao gold para permitir trace."""
    silver = _silver_with_person("mayor_x", "IN_SCOPE")
    sent, topic = _stub_sent_topic()

    gold = _silver_to_gold(article=silver, sent=sent, topic=topic, trend_score=0.0)

    assert gold.ingestion_run_id == "rss-test1234"
    assert gold.pipeline_version == "0.1.0-test"


# --- _write_silver_to_iceberg (Stage 3.7) ---


def test_write_silver_to_iceberg_noop_when_writer_none():
    df = pd.DataFrame({"content_hash": ["abc"], "title": ["t"]})
    _write_silver_to_iceberg(df, None, "batch-001")  # must not raise


def test_write_silver_to_iceberg_calls_append(monkeypatch):
    from unittest.mock import MagicMock

    writer = MagicMock()
    df = pd.DataFrame({"content_hash": ["abc"], "title": ["t"]})
    _write_silver_to_iceberg(df, writer, "batch-001")
    writer.append.assert_called_once()
    call_args = writer.append.call_args
    assert isinstance(call_args[0][0], pa.Table)
    assert call_args[0][1] == "silver_articles"


def test_write_silver_to_iceberg_swallows_exceptions(caplog):

    from unittest.mock import MagicMock

    writer = MagicMock()
    writer.append.side_effect = RuntimeError("disk full")
    df = pd.DataFrame({"x": [1]})
    _write_silver_to_iceberg(df, writer, "batch-err")  # must not raise
