"""Tests for ShadowConfig (Stage 1E v2 — warehouse persistence shadow)."""

from __future__ import annotations

import pytest

from mapear_infra.config import ShadowConfig


def test_defaults() -> None:
    cfg = ShadowConfig()
    assert cfg.rule_version_yaml == ""
    assert cfg.enabled is True


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPEAR_SHADOW_RULE_VERSION_YAML", "gs://b/candidate.yaml")
    monkeypatch.setenv("MAPEAR_SHADOW_ENABLED", "false")

    cfg = ShadowConfig()

    assert cfg.rule_version_yaml == "gs://b/candidate.yaml"
    assert cfg.enabled is False
