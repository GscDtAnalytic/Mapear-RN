"""Social-specific configuration extending mapear_infra.config.Settings.

Exposes ``APIFY_*`` env vars plus per-platform knobs (cron frequency,
per-run quotas, actor IDs). Keeps the same pattern as RSSSettings
so CD (cd-deploy.yml) and Cloud Run job envs stay uniform.
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from mapear_infra.config import Settings as CoreSettings


class ApifyConfig(BaseSettings):
    # Token from Apify Console — injected via Secret Manager in production.
    token: str = ""
    # Actor wall-clock budget. Apify kills the run after this; we surface it
    # here so retries in the pipeline align with the actor timeout.
    actor_timeout_seconds: int = 900
    # Max seconds to poll the run status endpoint before giving up.
    poll_timeout_seconds: int = 1200
    # Initial polling interval; the client applies a linear back-off up to 30s.
    poll_initial_interval_seconds: float = 3.0
    # Max items fetched per dataset page. Apify caps at 1000.
    dataset_page_size: int = 500
    # Daily per-platform budget (in posts). When exceeded, the pipeline
    # stops starting new runs — same pattern as RSS_FEED_FETCH_LIMIT.
    daily_post_budget_facebook: int = 5000
    daily_post_budget_instagram: int = 3500
    daily_post_budget_x: int = 8000
    daily_post_budget_tiktok: int = 3000

    model_config = {
        "env_prefix": "APIFY_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class SocialSettings(CoreSettings):
    """Settings for the mapear-social pipeline."""

    apify: ApifyConfig = Field(default_factory=ApifyConfig)

    # Platform the current run should scrape. Set via CLI (--platform) or
    # the SOCIAL_PLATFORM env var (Cloud Run Job argument).
    platform: str = "facebook"

    # Shadow mode: compute political sentiment label but do not block
    # Gold-bound rows on it. Used during calibration (W6-D1-2).
    political_sentiment_shadow: bool = True

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


def get_social_settings() -> SocialSettings:
    """Return Social-specific settings."""
    return SocialSettings()
