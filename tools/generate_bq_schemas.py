"""Regenerate `infra/modules/bigquery/schemas/*.json` from Pydantic.

Source of truth: `mapear-domain` Pydantic models + `mapear-storage` table
contracts (`mapear_storage.contracts.ARTICLE_CONTRACTS`).

Run via the Makefile:

    make schemas         # regenerate; show diff in stdout
    make schemas-check   # regenerate; fail if `git diff` is non-empty (CI gate)

When the Pydantic models change in a way that shifts the BQ schema, run
`make schemas` and commit the regenerated JSON in the same PR. The drift
test in `mapear-storage/tests/test_bq_schema_drift.py` and the CI gate
both enforce that the deployed JSON matches what the codegen produces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mapear_domain.schemas.bq_codegen import pydantic_to_bq_json
from mapear_storage.contracts import ARTICLE_CONTRACTS, TableContract

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "infra" / "modules" / "bigquery" / "schemas"


def _load_all_contracts() -> dict[str, TableContract]:
    """Collect contracts from every package that ships them.

    mapear-social provides its own ``SOCIAL_CONTRACTS``; if not installed
    (e.g. running this script from a env that lacks it), we skip and
    print a warning. Same shape as ``mapear-storage`` contracts.
    """
    contracts: dict[str, TableContract] = dict(ARTICLE_CONTRACTS)
    try:
        from mapear_social.contracts import SOCIAL_CONTRACTS

        contracts.update(SOCIAL_CONTRACTS)
    except ImportError:
        print(
            "warning: mapear-social not installed; "
            "social schemas will not be regenerated.",
            file=sys.stderr,
        )
    return contracts


def render(contract: TableContract) -> str:
    schema = pydantic_to_bq_json(
        contract.pydantic,
        permissive=contract.permissive,
        nullable_overrides=contract.nullable_overrides,
        field_order=list(contract.field_order) if contract.field_order else None,
    )
    return json.dumps(schema, indent=2) + "\n"


def main() -> int:
    if not SCHEMAS_DIR.exists():
        print(f"error: {SCHEMAS_DIR} not found", file=sys.stderr)
        return 2

    contracts = _load_all_contracts()
    written: list[str] = []
    for table, contract in contracts.items():
        path = SCHEMAS_DIR / f"{table}.json"
        new_content = render(contract)
        old_content = path.read_text() if path.exists() else None
        path.write_text(new_content)
        marker = "(unchanged)" if old_content == new_content else "(updated)"
        written.append(f"  {path.relative_to(REPO_ROOT)} {marker}")

    print("Regenerated BQ schemas from Pydantic:")
    for line in written:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
