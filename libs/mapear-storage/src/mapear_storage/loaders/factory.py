"""Factory functions to get the correct backend based on ENVIRONMENT."""

import os

from loguru import logger
from mapear_infra.config import get_settings

from mapear_storage.loaders.base import StorageWriter, WarehouseLoader


def _warn_common_env_mistakes() -> None:
    """Detect common env var naming mistakes and log a warning."""
    # People often set GCS_BUCKET_NAME instead of GCP_GCS_BUCKET_NAME
    if os.environ.get("GCS_BUCKET_NAME") and not os.environ.get("GCP_GCS_BUCKET_NAME"):
        logger.error(
            "Found GCS_BUCKET_NAME but NOT GCP_GCS_BUCKET_NAME. "
            "This project uses env prefix GCP_ — "
            "rename to GCP_GCS_BUCKET_NAME."
        )


def get_storage_writer() -> StorageWriter:
    """Return LocalWriter or GCSWriter based on environment."""
    if get_settings().is_local:
        from mapear_storage.loaders.local_writer import LocalWriter

        return LocalWriter()

    _warn_common_env_mistakes()

    from mapear_storage.loaders.gcs_writer import GCSWriter

    return GCSWriter()


def get_warehouse_loader() -> WarehouseLoader:
    """Return DuckDBLoader or BQLoader based on environment."""
    if get_settings().is_local:
        from mapear_storage.loaders.duckdb_loader import DuckDBLoader

        return DuckDBLoader()

    from mapear_storage.loaders.bq_loader import BQLoader

    return BQLoader()


def get_iceberg_writer():  # type: ignore[return]
    """Return an IcebergWriter when MAPEAR_ICEBERG_ENABLED=true, else None.

    Returns None when Iceberg is disabled so callers can guard with a
    simple ``if iceberg_writer:`` check without importing pyiceberg.
    """
    from mapear_infra.config import get_settings

    if not get_settings().iceberg.enabled:
        return None

    from mapear_storage.loaders.iceberg_writer import IcebergWriter

    return IcebergWriter.from_settings()


def get_pubsub_publisher():
    """Return a PubSubPublisher (always; disabled internally when unconfigured).

    Never raises — callers can always call publisher.publish_batch() and
    the no-op path handles the disabled case transparently.
    """
    from mapear_storage.pubsub_publisher import PubSubPublisher

    return PubSubPublisher.from_settings()
