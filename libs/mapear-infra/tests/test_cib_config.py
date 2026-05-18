"""Settings tests for CIBConfig — Eixo 3 v1."""

from __future__ import annotations

from mapear_infra.config import CIBConfig, Settings


def test_defaults_match_prod_thresholds() -> None:
    """Documents the prod-default behavior of the CIB gate."""
    cfg = CIBConfig()
    assert cfg.window_hours == 24.0
    assert cfg.min_overlap == 3
    assert cfg.enabled is True
    # Eixo 3 v2 — community detection defaults.
    assert cfg.community_algorithm == "louvain"
    assert cfg.community_resolution == 1.0
    assert cfg.community_seed == 42
    assert cfg.community_min_size == 3
    # Eixo 3 v2b — author resolution defaults.
    assert cfg.use_personas is False
    assert cfg.er_handle_similarity == 0.90
    assert cfg.er_display_name_similarity == 0.90
    assert cfg.er_min_shared_content == 1
    assert cfg.er_use_content_hash_bridge is True
    assert cfg.er_audit_enabled is True


def test_er_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("MAPEAR_CIB_USE_PERSONAS", "true")
    monkeypatch.setenv("MAPEAR_CIB_ER_HANDLE_SIMILARITY", "0.95")
    monkeypatch.setenv("MAPEAR_CIB_ER_DISPLAY_NAME_SIMILARITY", "0.85")
    monkeypatch.setenv("MAPEAR_CIB_ER_MIN_SHARED_CONTENT", "2")
    monkeypatch.setenv("MAPEAR_CIB_ER_USE_CONTENT_HASH_BRIDGE", "false")
    monkeypatch.setenv("MAPEAR_CIB_ER_AUDIT_ENABLED", "false")
    cfg = CIBConfig()
    assert cfg.use_personas is True
    assert cfg.er_handle_similarity == 0.95
    assert cfg.er_display_name_similarity == 0.85
    assert cfg.er_min_shared_content == 2
    assert cfg.er_use_content_hash_bridge is False
    assert cfg.er_audit_enabled is False


def test_community_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("MAPEAR_CIB_COMMUNITY_ALGORITHM", "label_propagation")
    monkeypatch.setenv("MAPEAR_CIB_COMMUNITY_RESOLUTION", "1.4")
    monkeypatch.setenv("MAPEAR_CIB_COMMUNITY_SEED", "7")
    monkeypatch.setenv("MAPEAR_CIB_COMMUNITY_MIN_SIZE", "5")
    cfg = CIBConfig()
    assert cfg.community_algorithm == "label_propagation"
    assert cfg.community_resolution == 1.4
    assert cfg.community_seed == 7
    assert cfg.community_min_size == 5


def test_env_overrides_window_hours(monkeypatch) -> None:
    monkeypatch.setenv("MAPEAR_CIB_WINDOW_HOURS", "48")
    monkeypatch.setenv("MAPEAR_CIB_MIN_OVERLAP", "5")
    monkeypatch.setenv("MAPEAR_CIB_ENABLED", "false")
    cfg = CIBConfig()
    assert cfg.window_hours == 48.0
    assert cfg.min_overlap == 5
    assert cfg.enabled is False


def test_settings_exposes_cib_namespace() -> None:
    settings = Settings()
    assert hasattr(settings, "cib")
    assert isinstance(settings.cib, CIBConfig)
    assert settings.cib.window_hours == 24.0


# ─── Eixo 3 v3 — scoring + cluster series ────────────────────────────────────


def test_v3_scoring_defaults() -> None:
    cfg = CIBConfig()
    assert cfg.score_sync_weight == 0.4
    assert cfg.score_jaccard_weight == 0.4
    assert cfg.score_content_sim_weight == 0.2
    assert cfg.score_sync_cap == 20.0
    assert cfg.cluster_series_threshold == 0.5


def test_v3_scoring_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("MAPEAR_CIB_SCORE_SYNC_WEIGHT", "0.5")
    monkeypatch.setenv("MAPEAR_CIB_SCORE_JACCARD_WEIGHT", "0.3")
    monkeypatch.setenv("MAPEAR_CIB_SCORE_CONTENT_SIM_WEIGHT", "0.2")
    monkeypatch.setenv("MAPEAR_CIB_SCORE_SYNC_CAP", "30.0")
    cfg = CIBConfig()
    assert cfg.score_sync_weight == 0.5
    assert cfg.score_jaccard_weight == 0.3
    assert cfg.score_content_sim_weight == 0.2
    assert cfg.score_sync_cap == 30.0


def test_v3_cluster_series_threshold_env_override(monkeypatch) -> None:
    monkeypatch.setenv("MAPEAR_CIB_CLUSTER_SERIES_THRESHOLD", "0.7")
    cfg = CIBConfig()
    assert cfg.cluster_series_threshold == 0.7
