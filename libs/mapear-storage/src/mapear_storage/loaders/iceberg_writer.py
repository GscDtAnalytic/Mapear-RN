"""Apache Iceberg table writer — Eixo 1 v1 (lakehouse foundation).

Uses PyIceberg with a SqlCatalog backend:
- Local: SQLite file (auto-created inside ``warehouse`` dir).
- Prod: Cloud SQL PostgreSQL DSN (reuses existing infra).

Warehouse is the root GCS URI (prod) or local filesystem path (dev)
where table data and metadata files are stored.

See docs/decisions/adr-eixo-1-v1-iceberg-foundation.md.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pyarrow as pa
from loguru import logger

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog


def _coerce_null_type(arrow_type: pa.DataType) -> pa.DataType:
    """Recursively replace pa.null() with pa.string() in an Arrow type."""
    if arrow_type == pa.null():
        return pa.string()
    if pa.types.is_list(arrow_type) or pa.types.is_large_list(arrow_type):
        new_value_type = _coerce_null_type(arrow_type.value_type)
        if new_value_type == arrow_type.value_type:
            return arrow_type
        return (
            pa.list_(new_value_type)
            if pa.types.is_list(arrow_type)
            else pa.large_list(new_value_type)
        )
    if pa.types.is_struct(arrow_type):
        new_fields = [
            pa.field(
                arrow_type.field(i).name, _coerce_null_type(arrow_type.field(i).type)
            )
            for i in range(arrow_type.num_fields)
        ]
        if all(
            new_fields[i].type == arrow_type.field(i).type
            for i in range(len(new_fields))
        ):
            return arrow_type
        return pa.struct(new_fields)
    return arrow_type


def _normalize_null_types(table: pa.Table) -> pa.Table:
    """Cast pa.null() columns/elements to pa.string().

    Iceberg format v2 rejects pa.null() — PyArrow infers null() when list
    columns are all-null in a batch (e.g., mentioned_mayors when no articles
    mention mayors).
    """
    needs_cast = False
    new_fields = []
    for field in table.schema:
        new_type = _coerce_null_type(field.type)
        if new_type != field.type:
            new_fields.append(field.with_type(new_type))
            needs_cast = True
        else:
            new_fields.append(field)
    if not needs_cast:
        return table
    return table.cast(pa.schema(new_fields))


def _normalize_timestamps(table: pa.Table) -> pa.Table:
    """Cast nanosecond timestamps to microseconds.

    PyIceberg's schema converter rejects timestamp[ns] — Iceberg only models
    time at microsecond granularity (TimestampType / TimestamptzType).
    pandas-to-Arrow conversion produces ns by default, so this is the common path.
    """
    needs_cast = False
    new_fields = []
    for field in table.schema:
        if pa.types.is_timestamp(field.type) and field.type.unit == "ns":
            new_fields.append(field.with_type(pa.timestamp("us", tz=field.type.tz)))
            needs_cast = True
        else:
            new_fields.append(field)
    if not needs_cast:
        return table
    return table.cast(pa.schema(new_fields))


def _build_catalog(warehouse: str, catalog_uri: str) -> Catalog:
    from pyiceberg.catalog.sql import SqlCatalog

    # Derive a SQLite URI when the caller does not provide one — safe for
    # local dev and tests.  Prod passes the PostgreSQL DSN explicitly.
    if not catalog_uri:
        if warehouse.startswith("gs://"):
            raise ValueError(
                "MAPEAR_ICEBERG_CATALOG_URI must be set for GCS warehouse. "
                "Use a PostgreSQL DSN (Cloud SQL) in production."
            )
        os.makedirs(warehouse, exist_ok=True)
        catalog_uri = f"sqlite:///{warehouse}/catalog.db"

    return SqlCatalog(
        "mapear",
        **{
            "uri": catalog_uri,
            "warehouse": warehouse,
        },
    )


class IcebergWriter:
    """Appends PyArrow tables to Iceberg tables on GCS or local filesystem.

    Tables are created on first write using the schema of the incoming
    pa.Table.  Subsequent appends must have a compatible schema — PyIceberg
    enforces this at the catalog level.

    When ``biglake_connection`` is provided and the warehouse is a GCS URI,
    each successful append also refreshes the corresponding BigQuery BigLake
    external table to point at the latest Iceberg metadata snapshot.

    Usage::

        writer = IcebergWriter.from_settings()
        writer.append(table, "silver_articles")
    """

    def __init__(
        self,
        catalog: Catalog,
        namespace: str,
        *,
        biglake_project: str = "",
        biglake_region: str = "",
        biglake_connection: str = "",
        biglake_dataset: str = "mapear_silver",
    ) -> None:
        self._catalog = catalog
        self._namespace = namespace
        self._biglake_project = biglake_project
        self._biglake_region = biglake_region
        self._biglake_connection = biglake_connection
        self._biglake_dataset = biglake_dataset
        self._ensure_namespace()

    def _ensure_namespace(self) -> None:
        try:
            self._catalog.create_namespace(self._namespace)
            logger.debug("Iceberg namespace created: {ns}", ns=self._namespace)
        except Exception:
            # Namespace already exists — this is the common path.
            pass

    def append(self, table: pa.Table, table_name: str) -> None:
        """Append ``table`` to the Iceberg table ``<namespace>.<table_name>``.

        Creates the Iceberg table on first call using ``table``'s schema.
        """
        from pyiceberg.exceptions import NoSuchTableError

        # _pyarrow_to_schema_without_ids is the correct converter for tables
        # being created from scratch (no pre-existing Iceberg field IDs).
        # pyarrow_to_schema is only for reading existing Iceberg Parquet files.
        from pyiceberg.io.pyarrow import _pyarrow_to_schema_without_ids

        table = _normalize_null_types(_normalize_timestamps(table))
        full_name = (self._namespace, table_name)

        try:
            iceberg_table = self._catalog.load_table(full_name)
            logger.debug(
                "Iceberg table exists, appending {rows} rows to {name}",
                rows=table.num_rows,
                name=f"{self._namespace}.{table_name}",
            )
        except NoSuchTableError:
            iceberg_schema = _pyarrow_to_schema_without_ids(table.schema)
            iceberg_table = self._catalog.create_table(
                full_name,
                schema=iceberg_schema,
            )
            logger.info(
                "Iceberg table created: {name} ({fields} fields)",
                name=f"{self._namespace}.{table_name}",
                fields=len(table.schema),
            )

        iceberg_table.append(table)
        logger.info(
            "Iceberg append: {rows} rows → {name}",
            rows=table.num_rows,
            name=f"{self._namespace}.{table_name}",
        )
        self._refresh_biglake_table(table_name, iceberg_table.metadata_location)

    def _refresh_biglake_table(self, table_name: str, metadata_location: str) -> None:
        """Update the BigLake external table to point at the latest Iceberg snapshot.

        No-op when ``biglake_connection`` is empty or ``metadata_location``
        is not a GCS URI (local dev). Never raises — a DDL failure is logged
        as a warning and does not affect the Iceberg write result.
        """
        if not self._biglake_connection:
            return
        if not metadata_location.startswith("gs://"):
            return
        try:
            from google.cloud import bigquery

            bq_table = (
                f"{self._biglake_project}.{self._biglake_dataset}.{table_name}_iceberg"
            )
            connection = (
                f"{self._biglake_project}"
                f".{self._biglake_region}"
                f".{self._biglake_connection}"
            )
            ddl = (
                f"CREATE OR REPLACE EXTERNAL TABLE `{bq_table}`\n"
                f"WITH CONNECTION `{connection}`\n"
                f"OPTIONS (format = 'ICEBERG', uris = ['{metadata_location}']);"
            )
            bigquery.Client(project=self._biglake_project or None).query(ddl).result()
            logger.info(
                "BigLake refresh: {table} → {meta}",
                table=bq_table,
                meta=metadata_location,
            )
        except Exception as exc:
            logger.warning(
                "BigLake refresh failed for {t}: {err}", t=table_name, err=exc
            )

    @classmethod
    def from_settings(cls) -> IcebergWriter:
        from mapear_infra.config import get_settings

        settings = get_settings()
        cfg = settings.iceberg
        gcp = settings.gcp
        if not cfg.warehouse:
            raise ValueError(
                "MAPEAR_ICEBERG_WAREHOUSE must be set when "
                "MAPEAR_ICEBERG_ENABLED=true."
            )
        catalog = _build_catalog(cfg.warehouse, cfg.catalog_uri)
        return cls(
            catalog,
            cfg.namespace,
            biglake_project=gcp.project_id,
            biglake_region=gcp.region,
            biglake_connection=cfg.biglake_connection,
            biglake_dataset=cfg.biglake_dataset,
        )
