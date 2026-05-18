"""Codegen `pydantic_to_bq_json` produces the deployed BQ schemas verbatim.

Parses both the generated and on-disk JSON into Python objects and compares
those — so the test passes regardless of whitespace/column-alignment in
the source file. The byte-level guarantee is enforced separately by
`make schemas-check` (regenerate + git diff).

Reads the deployed Terraform JSON from `infra/modules/bigquery/schemas/`
relative to the monorepo root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mapear_domain.models.base import GoldArticle, RawArticle, SilverArticle
from mapear_domain.schemas.bq_codegen import pydantic_to_bq_json


def _schemas_dir() -> Path:
    """Locate infra/modules/bigquery/schemas by walking up from this file —
    stable regardless of how deep the package sits in the monorepo."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "infra" / "modules" / "bigquery" / "schemas"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("BigQuery schemas directory not found")


SCHEMAS_DIR = _schemas_dir()

# Two V1 canonical computed fields that must accept NULL even though the
# Pydantic property is non-Optional. See base.py and the legacy comment in
# parquet_writer.py: "nullable because older pipeline versions may not
# populate them."
_V1_NULLABLE = frozenset({"content_rn_relevant", "author_in_scope"})

# The deployed silver_articles BQ schema places the V1 computed fields
# between `resolution_confidence` and `actor_run_id`. The default Pydantic
# order (model_fields, then model_computed_fields) puts them at the end —
# so an explicit field_order is required.
#
# Canonical source of truth for this constant is
# `mapear_storage.contracts._SILVER_FIELD_ORDER`. mapear-domain is a leaf
# package and cannot import from mapear-storage, so the list is mirrored
# here — keep both in sync when adding/reordering fields.
_SILVER_FIELD_ORDER: list[str] = [
    "url",
    "source_feed",
    "title",
    "content_clean",
    "author",
    "published_at",
    "extracted_at",
    "content_hash",
    "entities",
    "mentioned_cities",
    "mentioned_mayors",
    "mentioned_governors",
    "mentioned_parties",
    "mentioned_persons",
    "is_rn_relevant",
    "source_type",
    "schema_version",
    "person_id",
    "scope_status",
    "resolution_confidence",
    "content_rn_relevant",
    "author_in_scope",
    "actor_run_id",
    "ingestion_run_id",
    "rule_version",
    "pipeline_version",
    # Stage 2B — tenant_id is the last lineage stamp.
    "tenant_id",
]


@pytest.mark.parametrize(
    "table, generator",
    [
        (
            "raw_articles",
            lambda: pydantic_to_bq_json(RawArticle, permissive=True),
        ),
        (
            "silver_articles",
            lambda: pydantic_to_bq_json(
                SilverArticle,
                field_order=_SILVER_FIELD_ORDER,
                # `source_type` was added late and historic silver rows lack it.
                nullable_overrides=_V1_NULLABLE | {"source_type"},
            ),
        ),
        (
            "gold_articles",
            lambda: pydantic_to_bq_json(
                GoldArticle,
                # topic_label defaults to "" but BQ accepts NULL for legacy.
                nullable_overrides=_V1_NULLABLE | {"topic_label"},
            ),
        ),
    ],
)
def test_codegen_matches_deployed(table: str, generator) -> None:
    on_disk = json.loads((SCHEMAS_DIR / f"{table}.json").read_text())
    generated = generator()
    assert generated == on_disk, (
        f"BQ codegen for `{table}` diverges from deployed schema.\n"
        "  Either update the Pydantic model or adjust the generator config "
        "(field_order/nullable_overrides) in mapear-storage."
    )
