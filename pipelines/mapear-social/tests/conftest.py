"""Shared pytest fixtures for mapear-social."""

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    """Return a JSON fixture from tests/fixtures/<name>.json."""

    def _load(name: str) -> list[dict] | dict:
        path = FIXTURES_DIR / f"{name}.json"
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    return _load
