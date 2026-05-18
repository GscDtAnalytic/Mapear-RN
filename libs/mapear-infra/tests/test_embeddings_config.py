"""Settings tests for EmbeddingsConfig — Eixo 2 v2a."""

from __future__ import annotations

from mapear_infra.config import EmbeddingsConfig, Settings


def test_defaults_match_prod_thresholds() -> None:
    """Documents the prod-default behavior of the embedding + clustering gate."""
    cfg = EmbeddingsConfig()
    assert cfg.model == "paraphrase-multilingual-mpnet-base-v2"
    assert cfg.cache_enabled is True
    assert cfg.cache_gcs_prefix == "narrative_embeddings/"
    # Clustering defaults.
    assert cfg.cluster_algorithm == "hdbscan"
    assert cfg.cluster_min_size == 3
    assert cfg.cluster_distance_metric == "cosine"
    assert cfg.cluster_cosine_threshold == 0.75
    assert cfg.cluster_enabled is True


def test_env_overrides_embedding(monkeypatch) -> None:
    monkeypatch.setenv("MAPEAR_EMBEDDINGS_MODEL", "intfloat/multilingual-e5-large")
    monkeypatch.setenv("MAPEAR_EMBEDDINGS_CACHE_ENABLED", "false")
    monkeypatch.setenv("MAPEAR_EMBEDDINGS_CACHE_GCS_PREFIX", "custom_cache/")
    cfg = EmbeddingsConfig()
    assert cfg.model == "intfloat/multilingual-e5-large"
    assert cfg.cache_enabled is False
    assert cfg.cache_gcs_prefix == "custom_cache/"


def test_env_overrides_clustering(monkeypatch) -> None:
    monkeypatch.setenv("MAPEAR_EMBEDDINGS_CLUSTER_ALGORITHM", "cosine_threshold")
    monkeypatch.setenv("MAPEAR_EMBEDDINGS_CLUSTER_MIN_SIZE", "5")
    monkeypatch.setenv("MAPEAR_EMBEDDINGS_CLUSTER_COSINE_THRESHOLD", "0.82")
    monkeypatch.setenv("MAPEAR_EMBEDDINGS_CLUSTER_ENABLED", "false")
    cfg = EmbeddingsConfig()
    assert cfg.cluster_algorithm == "cosine_threshold"
    assert cfg.cluster_min_size == 5
    assert cfg.cluster_cosine_threshold == 0.82
    assert cfg.cluster_enabled is False


def test_settings_exposes_embeddings_namespace() -> None:
    settings = Settings()
    assert hasattr(settings, "embeddings")
    assert isinstance(settings.embeddings, EmbeddingsConfig)
    assert settings.embeddings.model == "paraphrase-multilingual-mpnet-base-v2"
