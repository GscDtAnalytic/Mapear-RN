"""Settings tests for LLMConfig — Eixo 2 v1 + v2b (stance)."""

from __future__ import annotations

from mapear_infra.config import LLMConfig, Settings


def test_stance_defaults() -> None:
    """Documents the prod-default behavior of the stance gate."""
    cfg = LLMConfig()
    assert cfg.stance_enabled is True
    assert cfg.stance_cache_gcs_prefix == "narrative_stance/"


def test_stance_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("MAPEAR_LLM_STANCE_ENABLED", "false")
    monkeypatch.setenv("MAPEAR_LLM_STANCE_CACHE_GCS_PREFIX", "custom_stance/")
    cfg = LLMConfig()
    assert cfg.stance_enabled is False
    assert cfg.stance_cache_gcs_prefix == "custom_stance/"


def test_settings_exposes_llm_namespace() -> None:
    settings = Settings()
    assert hasattr(settings, "llm")
    assert isinstance(settings.llm, LLMConfig)
    assert settings.llm.stance_enabled is True
    assert settings.llm.stance_cache_gcs_prefix == "narrative_stance/"
