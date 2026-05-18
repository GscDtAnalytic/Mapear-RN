"""RSS-specific configuration extending mapear_infra.config.Settings."""

from pydantic import Field
from pydantic_settings import BaseSettings

from mapear_infra.config import Settings as CoreSettings

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def get_default_headers(user_agent: str) -> dict[str, str]:
    """Return realistic browser headers for HTTP requests."""
    return {
        "User-Agent": user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def get_feed_headers(user_agent: str) -> dict[str, str]:
    """Return headers optimized for RSS/Atom feed requests."""
    return {
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.5",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    }


class ScraperConfig(BaseSettings):
    max_workers: int = 5
    delay_min: float = 2.0
    delay_max: float = 5.0
    user_agent: str = DEFAULT_USER_AGENT
    respect_robots_txt: bool = True

    # --- Diagnostics & block detection -----------------------------------
    log_level: str = "INFO"
    diagnostic_sample_rate: float = 1.0
    block_detection_enabled: bool = True
    # comma-separated, e.g. "blogdobg.com.br,agorarn.com.br"
    debug_domains: str = ""

    # --- Adaptive retry / cooldown ---------------------------------------
    # bot_block class (403, anti-bot signals): 5 min base, 30 min cap.
    # Cooldown is in-memory (resets each run). The run scrapes for ~2 min,
    # so a 2h+ cooldown effectively skips the domain for the entire run.
    # Keeping it short allows retries within the same batch.
    domain_cooldown_seconds: float = 300.0
    domain_cooldown_max_seconds: float = 1800.0
    max_retries_blocked: int = 1
    max_retries_transient: int = 3
    # Consecutive qualifying blocks required before the first bot_block
    # window is armed. A single false positive should not park a domain.
    cooldown_trigger_threshold: int = 2
    # rate_limit class (HTTP 429): 5 min base, 15 min cap.
    cooldown_rate_limit_base_seconds: float = 300.0
    cooldown_rate_limit_max_seconds: float = 900.0
    # parser_hard class (selector_missing, empty_content): 6h park after
    # N=cooldown_parser_hard_threshold consecutive all-layer failures.
    # Shorter than the old 24h default because Camoufox now also gets a
    # chance before the cooldown arms, so reaching N is rarer.
    cooldown_parser_hard_seconds: float = 21600.0
    # Consecutive full-stack (httpx + browser) selector_missing failures
    # required before parking the domain. Default 3.
    cooldown_parser_hard_threshold: int = 3
    # Legacy flag: when True, the generic "parser" class (parser_failure
    # crashes) never parks the domain. parser_hard ignores this flag.
    cooldown_parser_disabled: bool = True

    # --- Frontier recirculation ------------------------------------------
    # When the initial pending queue is empty but discovery returned URLs,
    # the pipeline attempts to recirculate previously completed URLs that
    # have aged past this TTL so it never sits fully idle.
    frontier_enable_recirculation: bool = True
    frontier_reprocess_ttl_hours: int = 72
    frontier_recirculation_limit: int = 200
    frontier_recirculate_include_failed: bool = False

    # --- User-Agent rotation & jitter ------------------------------------
    ua_rotation_enabled: bool = True
    inter_request_jitter_ms_min: int = 0
    inter_request_jitter_ms_max: int = 0

    # --- Camoufox headless fallback (preferred over Playwright) ----------
    # Camoufox is a patched Firefox that removes all browser-automation
    # indicators (navigator.webdriver, CDP fingerprint, canvas metrics).
    # Fires for any domain where httpx returns a genuine bot-block signal
    # (challenge body markers), not just CF-CDN header presence.
    # Install: pip install "camoufox[geoip]" && python -m camoufox fetch
    camoufox_enabled: bool = False
    camoufox_max_concurrent: int = 2
    camoufox_timeout_ms: int = 20000

    # --- Playwright headless fallback (legacy; Camoufox preferred) -------
    # Opt-in render-based fallback for the handful of RN portals that
    # serve Cloudflare JS challenges / WAF walls to the Cloud Run egress.
    # Only fires for domains in ``playwright_targeted_domains`` AND only
    # after the httpx path has exhausted its bot_block retries.
    playwright_enabled: bool = False
    playwright_browser: str = "firefox"  # firefox | chromium
    # Bumped from 15s: Camada 1 warm-up (home visit + 2-4s idle) adds ~5s
    # to the first fetch on a cold domain; 20s leaves room for a slow CF
    # edge without false timeouts.
    playwright_timeout_ms: int = 20000
    playwright_max_concurrent: int = 2
    playwright_targeted_domains: str = (
        "blogdobg.com.br,www.blogdobg.com.br,"
        "agorarn.com.br,www.agorarn.com.br,"
        "tribunadonorte.com.br,www.tribunadonorte.com.br,"
        "saibamais.jor.br,www.saibamais.jor.br,"
        "novonoticias.com,www.novonoticias.com.br"
    )
    # --- Camada 1 stealth reinforcements ---------------------------------
    # When true, launch the browser with headless=False. Requires a
    # working X display ($DISPLAY set) — on Linux containers that means
    # Xvfb must be running ahead of time (e.g. ``xvfb-run -a python -m
    # mapear_rss``). Headed Firefox with Xvfb is materially harder to
    # detect than headless because the absence-of-display signal is the
    # single biggest stealth tell on Linux. Falls back to headless if
    # $DISPLAY is missing, with a warning.
    playwright_headed: bool = False
    # Default Firefox UA used when the UA rotator returns a non-Firefox
    # UA but the browser is Firefox. Kept in config so rotation updates
    # don't need to touch BrowserScraper. Bump this when Firefox ESR/
    # stable moves — stale UAs are themselves a fingerprint.
    playwright_default_firefox_ua: str = (
        "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) " "Gecko/20100101 Firefox/134.0"
    )
    playwright_locale: str = "pt-BR"
    playwright_timezone: str = "America/Fortaleza"
    # Warm-up: before the target URL, visit the domain root, idle 2-4s,
    # then navigate. Mirrors a human opening a tab and glancing at the
    # homepage, and lets the site set its anti-bot cookies
    # (``__cf_bm``, ``datadome``) on a lenient request before we ask for
    # the juicier article endpoint. Warm-up happens at most once per
    # domain per run; subsequent fetches reuse the storage_state.
    playwright_warmup_enabled: bool = True
    playwright_warmup_wait_ms_min: int = 2000
    playwright_warmup_wait_ms_max: int = 4000

    def playwright_targeted_domain_set(self) -> frozenset[str]:
        if not self.playwright_targeted_domains:
            return frozenset()
        return frozenset(
            d.strip() for d in self.playwright_targeted_domains.split(",") if d.strip()
        )

    model_config = {
        "env_prefix": "SCRAPER_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    def debug_domain_set(self) -> frozenset[str]:
        if not self.debug_domains:
            return frozenset()
        return frozenset(d.strip() for d in self.debug_domains.split(",") if d.strip())


class CircuitBreakerConfig(BaseSettings):
    failure_threshold: int = 5
    recovery_timeout: int = 300
    half_open_requests: int = 2

    model_config = {
        "env_prefix": "CB_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class FeedHealthConfig(BaseSettings):
    """Config for feed health pre-checks before discovery."""

    enabled: bool = True
    timeout_s: float = 10.0
    # Emit ERROR and flag unhealthy when consecutive failures >= this value
    consecutive_failure_threshold: int = 3

    model_config = {
        "env_prefix": "FEED_HEALTH_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class DiversityConfig(BaseSettings):
    """Config for source concentration scoring and alerting."""

    # Share threshold above which a single source triggers a WARNING.
    # 0.70 → alert when one source accounts for >70 % of the batch.
    concentration_threshold: float = 0.70

    model_config = {
        "env_prefix": "DIVERSITY_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


class RSSSettings(CoreSettings):
    """Settings for the RSS pipeline, extending core with scraper configs."""

    scraper: ScraperConfig = Field(default_factory=ScraperConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    feed_health: FeedHealthConfig = Field(default_factory=FeedHealthConfig)
    diversity: DiversityConfig = Field(default_factory=DiversityConfig)


def get_rss_settings() -> RSSSettings:
    """Return RSS-specific settings."""
    return RSSSettings()
