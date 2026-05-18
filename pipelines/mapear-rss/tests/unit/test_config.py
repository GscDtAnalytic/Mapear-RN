"""Tests for application configuration."""

from mapear_infra.config import Environment
from mapear_rss.config import get_rss_settings as get_settings


class TestSettings:
    def test_default_is_local(self) -> None:
        settings = get_settings()
        assert settings.environment == Environment.LOCAL
        assert settings.is_local is True

    def test_lake_paths(self) -> None:
        settings = get_settings()
        assert "raw" in str(settings.lake_raw)
        assert "silver" in str(settings.lake_silver)
        assert "gold" in str(settings.lake_gold)

    def test_postgres_dsn(self) -> None:
        settings = get_settings()
        dsn = settings.postgres.dsn
        assert "postgresql+psycopg2://" in dsn
