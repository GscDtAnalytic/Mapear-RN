"""Codegen `pydantic_to_arrow` reproduces the deployed Arrow schemas.

Compares against frozen fixtures captured from the original hand-coded
`pa.Schema` constants in `parquet_writer.py` before the codegen migration
(see `tests/fixtures/golden/*.arrow.txt`).

The comparison is `str(schema)` text equality. pyarrow's repr is stable,
deterministic, and human-readable — adequate for catching any drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mapear_social.contracts import SOCIAL_CONTRACTS

from mapear_storage.contracts import ARTICLE_CONTRACTS, TableContract
from mapear_storage.loaders.arrow_codegen import pydantic_to_arrow

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"


def _arrow_from_contract(c: TableContract):
    return pydantic_to_arrow(
        c.pydantic,
        permissive=c.permissive,
        nullable_overrides=c.nullable_overrides,
        field_order=list(c.field_order) if c.field_order else None,
    )


_ALL_GENERATORS = [
    (table, (lambda c=contract: _arrow_from_contract(c)))
    for table, contract in {**ARTICLE_CONTRACTS, **SOCIAL_CONTRACTS}.items()
]


@pytest.mark.parametrize("table, generator", _ALL_GENERATORS)
def test_arrow_codegen_matches_golden(table: str, generator) -> None:
    expected = (GOLDEN_DIR / f"{table}.arrow.txt").read_text().strip()
    actual = str(generator()).strip()
    assert actual == expected, (
        f"Arrow codegen for `{table}` diverges from the golden fixture.\n"
        "  Either update the Pydantic model or adjust the generator config.\n"
        "  If the change is intentional, regenerate the fixture by running\n"
        "  the snapshot script under mapear-storage."
    )
