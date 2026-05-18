"""BQ codegen for social tables matches the deployed schemas.

Mirrors `mapear-domain/tests/test_bq_codegen.py` but for the social
warehouse tables. Lives here because `SilverSocialPost`,
`SocialPostDLQ`, and `SocialPost` are mapear-social Pydantic models.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mapear_domain.schemas.bq_codegen import pydantic_to_bq_json
from mapear_social.contracts import SOCIAL_CONTRACTS


def _schemas_dir() -> Path:
    """Locate infra/modules/bigquery/schemas by walking up from this file —
    stable regardless of how deep the package sits in the monorepo."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "infra" / "modules" / "bigquery" / "schemas"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("BigQuery schemas directory not found")


SCHEMAS_DIR = _schemas_dir()


@pytest.mark.parametrize("table", list(SOCIAL_CONTRACTS))
def test_codegen_matches_deployed(table: str) -> None:
    contract = SOCIAL_CONTRACTS[table]
    on_disk = json.loads((SCHEMAS_DIR / f"{table}.json").read_text())
    generated = pydantic_to_bq_json(
        contract.pydantic,
        permissive=contract.permissive,
        nullable_overrides=contract.nullable_overrides,
        field_order=list(contract.field_order) if contract.field_order else None,
    )
    assert generated == on_disk, (
        f"BQ codegen for `{table}` diverges from deployed schema.\n"
        "  Either update the Pydantic model or adjust the SOCIAL_CONTRACTS entry."
    )
