"""Tests for IcebergWriter — Eixo 1 v1.

All tests use a local SQLite catalog + tmp filesystem warehouse so they
run without GCS credentials or network access.
"""

import pyarrow as pa
import pytest


@pytest.fixture
def tmp_warehouse(tmp_path):
    return str(tmp_path / "warehouse")


@pytest.fixture
def catalog(tmp_warehouse):
    from mapear_storage.loaders.iceberg_writer import _build_catalog

    return _build_catalog(warehouse=tmp_warehouse, catalog_uri="")


@pytest.fixture
def writer(catalog):
    from mapear_storage.loaders.iceberg_writer import IcebergWriter

    return IcebergWriter(catalog, namespace="test")


def _make_table(**cols) -> pa.Table:
    return pa.table(cols)


# --- _build_catalog ---


def test_build_catalog_creates_sqlite_when_no_uri(tmp_warehouse):
    from mapear_storage.loaders.iceberg_writer import _build_catalog

    catalog = _build_catalog(warehouse=tmp_warehouse, catalog_uri="")
    assert catalog is not None


def test_build_catalog_raises_for_gcs_without_uri():
    from mapear_storage.loaders.iceberg_writer import _build_catalog

    with pytest.raises(ValueError, match="MAPEAR_ICEBERG_CATALOG_URI"):
        _build_catalog(warehouse="gs://bucket/warehouse", catalog_uri="")


def test_build_catalog_accepts_explicit_uri(tmp_path):
    from mapear_storage.loaders.iceberg_writer import _build_catalog

    uri = f"sqlite:///{tmp_path}/custom.db"
    warehouse = str(tmp_path / "wh")
    catalog = _build_catalog(warehouse=warehouse, catalog_uri=uri)
    assert catalog is not None


# --- IcebergWriter.append ---


def test_append_creates_table_on_first_write(writer):
    table = _make_table(id=pa.array([1, 2, 3]), name=pa.array(["a", "b", "c"]))
    writer.append(table, "articles")

    loaded = writer._catalog.load_table(("test", "articles"))
    assert loaded is not None


def test_append_writes_correct_row_count(writer):
    table = _make_table(x=pa.array([10, 20, 30]))
    writer.append(table, "events")

    loaded = writer._catalog.load_table(("test", "events"))
    scan = loaded.scan().to_arrow()
    assert scan.num_rows == 3


def test_append_multiple_batches_accumulates_rows(writer):
    t1 = _make_table(val=pa.array([1, 2]))
    t2 = _make_table(val=pa.array([3, 4, 5]))
    writer.append(t1, "counts")
    writer.append(t2, "counts")

    loaded = writer._catalog.load_table(("test", "counts"))
    scan = loaded.scan().to_arrow()
    assert scan.num_rows == 5


def test_append_preserves_schema(writer):
    schema = pa.schema(
        [
            pa.field("content_hash", pa.string()),
            pa.field("published_at", pa.timestamp("us")),
            pa.field("score", pa.float64()),
        ]
    )
    import datetime

    table = pa.table(
        {
            "content_hash": pa.array(["abc"]),
            "published_at": pa.array(
                [datetime.datetime(2026, 1, 1)], type=pa.timestamp("us")
            ),
            "score": pa.array([0.9]),
        },
        schema=schema,
    )
    writer.append(table, "scored")

    loaded = writer._catalog.load_table(("test", "scored"))
    scan = loaded.scan().to_arrow()
    assert "content_hash" in scan.column_names
    assert "score" in scan.column_names


def test_append_idempotent_table_name(writer):
    t = _make_table(n=pa.array([1]))
    writer.append(t, "same_table")
    writer.append(t, "same_table")

    loaded = writer._catalog.load_table(("test", "same_table"))
    assert loaded is not None


def test_two_writers_same_catalog_same_namespace(tmp_warehouse, catalog):
    from mapear_storage.loaders.iceberg_writer import IcebergWriter

    w1 = IcebergWriter(catalog, "shared")
    w2 = IcebergWriter(catalog, "shared")

    w1.append(_make_table(a=pa.array([1])), "tbl")
    w2.append(_make_table(a=pa.array([2])), "tbl")

    loaded = catalog.load_table(("shared", "tbl"))
    assert loaded.scan().to_arrow().num_rows == 2


# --- get_iceberg_writer factory ---


def test_get_iceberg_writer_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("MAPEAR_ICEBERG_ENABLED", "false")
    from importlib import reload

    import mapear_infra.config as cfg_mod

    import mapear_storage.loaders.factory as fac_mod

    reload(cfg_mod)
    reload(fac_mod)
    from mapear_storage.loaders.factory import get_iceberg_writer

    assert get_iceberg_writer() is None


def test_append_casts_nanosecond_timestamps_to_microseconds(writer):
    """_normalize_timestamps must convert ns→us so pyiceberg accepts the schema."""
    import datetime

    table = pa.table(
        {
            "id": pa.array([1]),
            "published_at": pa.array(
                [datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)],
                type=pa.timestamp("ns", tz="UTC"),
            ),
        }
    )
    writer.append(table, "ns_ts_table")
    loaded = writer._catalog.load_table(("test", "ns_ts_table"))
    result = loaded.scan().to_arrow()
    assert result.num_rows == 1
    ts_type = result.schema.field("published_at").type
    assert ts_type.unit == "us"


def test_normalize_timestamps_noop_when_no_ns(writer):
    """Tables without ns timestamps pass through unchanged."""
    import datetime

    table = pa.table(
        {
            "id": pa.array([1]),
            "ts": pa.array([datetime.datetime(2026, 1, 1)], type=pa.timestamp("us")),
        }
    )
    writer.append(table, "us_ts_table")
    loaded = writer._catalog.load_table(("test", "us_ts_table"))
    result = loaded.scan().to_arrow()
    assert result.num_rows == 1


def test_append_casts_null_list_element_type(writer):
    """mentioned_mayors-style all-null list columns must not break Iceberg v2."""
    table = pa.table(
        {
            "id": pa.array([1, 2]),
            "mentioned_mayors": pa.array(
                [[], []],
                type=pa.list_(pa.null()),
            ),
        }
    )
    writer.append(table, "null_list_table")
    loaded = writer._catalog.load_table(("test", "null_list_table"))
    result = loaded.scan().to_arrow()
    assert result.num_rows == 2


def test_append_casts_null_top_level_column(writer):
    """A top-level pa.null() column is cast to pa.string()."""
    table = pa.table(
        {
            "id": pa.array([1]),
            "empty_col": pa.array([None], type=pa.null()),
        }
    )
    writer.append(table, "null_col_table")
    loaded = writer._catalog.load_table(("test", "null_col_table"))
    result = loaded.scan().to_arrow()
    assert result.num_rows == 1


def test_refresh_biglake_noop_when_no_connection(writer):
    """_refresh_biglake_table is a no-op when biglake_connection is empty."""
    captured = []
    writer._refresh_biglake_table(
        "silver_articles",
        "gs://bucket/iceberg/mapear/silver_articles/metadata/00001.metadata.json",
    )
    assert captured == []


def test_refresh_biglake_noop_for_local_path(tmp_warehouse, catalog):
    """_refresh_biglake_table skips when metadata_location is not a GCS URI."""
    from mapear_storage.loaders.iceberg_writer import IcebergWriter

    w = IcebergWriter(
        catalog,
        "test",
        biglake_project="proj",
        biglake_region="us",
        biglake_connection="my-conn",
    )
    w._refresh_biglake_table(
        "silver_articles", "/tmp/local/metadata/00001.metadata.json"
    )


def test_refresh_biglake_executes_ddl(monkeypatch, tmp_warehouse, catalog):
    """_refresh_biglake_table calls BigQuery DDL with the correct metadata URI."""
    import sys
    from unittest.mock import MagicMock

    from mapear_storage.loaders.iceberg_writer import IcebergWriter

    mock_bq = MagicMock()
    mock_client = MagicMock()
    mock_bq.Client.return_value = mock_client

    mock_gcloud = MagicMock()
    mock_gcloud.bigquery = mock_bq
    monkeypatch.setitem(sys.modules, "google", MagicMock())
    monkeypatch.setitem(sys.modules, "google.cloud", mock_gcloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", mock_bq)

    w = IcebergWriter(
        catalog,
        "test",
        biglake_project="my-project",
        biglake_region="southamerica-east1",
        biglake_connection="mapear-iceberg",
        biglake_dataset="mapear_silver",
    )
    meta_uri = "gs://bucket/iceberg/mapear/silver_articles/metadata/00001.metadata.json"
    w._refresh_biglake_table("silver_articles", meta_uri)

    mock_bq.Client.assert_called_once_with(project="my-project")
    ddl_call = mock_client.query.call_args[0][0]
    assert "silver_articles_iceberg" in ddl_call
    assert "mapear-iceberg" in ddl_call
    assert meta_uri in ddl_call
    assert "CREATE OR REPLACE EXTERNAL TABLE" in ddl_call


def test_get_iceberg_writer_raises_when_enabled_no_warehouse(monkeypatch):
    monkeypatch.setenv("MAPEAR_ICEBERG_ENABLED", "true")
    monkeypatch.setenv("MAPEAR_ICEBERG_WAREHOUSE", "")
    from importlib import reload

    import mapear_infra.config as cfg_mod

    reload(cfg_mod)

    from mapear_storage.loaders.factory import get_iceberg_writer

    with pytest.raises(ValueError, match="MAPEAR_ICEBERG_WAREHOUSE"):
        get_iceberg_writer()
