"""Web scraper with rate limiting, robots.txt compliance, and circuit breaker.

Supports both sync and async scraping. The async path uses httpx.AsyncClient
with per-domain semaphores to respect rate limits while maximizing throughput.

The async path is fully instrumented with diagnostics, block detection,
adaptive retry and per-domain cooldown so each run produces evidence for
'blocked vs parser failure' without leaking response bodies.
"""

import asyncio
import random
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from loguru import logger

from mapear_domain.models.base import RawArticle
from mapear_infra.retry import retry_on_network_error
from mapear_rss.config import get_default_headers, get_rss_settings
from mapear_rss.extraction.article_parser import ArticleParser
from mapear_rss.extraction.block_detector import (
    BlockSignals,
    classify_failure,
    detect,
)
from mapear_rss.extraction.browser_scraper import BrowserFetchResult, BrowserScraper
from mapear_rss.extraction.camoufox_scraper import CamoufoxScraper
from mapear_rss.extraction.circuit_breaker import (
    CircuitBreakerProtocol,
    build_circuit_breaker,
)
from mapear_rss.extraction.diagnostics import (
    BODY_SAMPLE_BYTES,
    DiagnosticCollector,
    DiagnosticRecord,
    filter_headers,
    hash_body_sample,
)
from mapear_rss.extraction.domain_cooldown import (
    DomainCooldown,
    error_class_for,
    retry_budget,
)
from mapear_rss.extraction.httpx_scraper import HttpxScraper
from mapear_rss.extraction.user_agents import UserAgentRotator


class Scraper:
    """Fetches and parses web pages into RawArticle objects."""

    def __init__(
        self,
        circuit_breaker: CircuitBreakerProtocol | None = None,
        diagnostics: DiagnosticCollector | None = None,
        cooldown: DomainCooldown | None = None,
        ua_rotator: UserAgentRotator | None = None,
        browser: BrowserScraper | None = None,
    ) -> None:
        settings = get_rss_settings()
        self.settings = settings
        self.user_agent = settings.scraper.user_agent
        self.delay_min = settings.scraper.delay_min
        self.delay_max = settings.scraper.delay_max
        self.respect_robots = settings.scraper.respect_robots_txt
        self.timeout = 30.0
        self.max_concurrent = settings.scraper.max_workers
        self.block_detection_enabled = settings.scraper.block_detection_enabled
        self.jitter_min_ms = settings.scraper.inter_request_jitter_ms_min
        self.jitter_max_ms = settings.scraper.inter_request_jitter_ms_max

        self.cb = circuit_breaker or build_circuit_breaker()

        self.diagnostics = diagnostics or DiagnosticCollector(
            sample_rate=settings.scraper.diagnostic_sample_rate,
            debug_domains=settings.scraper.debug_domain_set(),
        )
        self.parser = ArticleParser(on_recovery=self.diagnostics.note_parser_recovery)
        self.cooldown = cooldown or DomainCooldown(
            base_seconds=settings.scraper.domain_cooldown_seconds,
            max_seconds=settings.scraper.domain_cooldown_max_seconds,
            rate_limit_base_seconds=settings.scraper.cooldown_rate_limit_base_seconds,
            rate_limit_max_seconds=settings.scraper.cooldown_rate_limit_max_seconds,
            parser_hard_seconds=settings.scraper.cooldown_parser_hard_seconds,
            trigger_threshold=settings.scraper.cooldown_trigger_threshold,
            parser_hard_trigger=settings.scraper.cooldown_parser_hard_threshold,
            parser_disabled=settings.scraper.cooldown_parser_disabled,
        )
        self.ua_rotator = ua_rotator or UserAgentRotator(
            enabled=settings.scraper.ua_rotation_enabled,
        )
        self._retry_overrides = {
            "blocked_bot": settings.scraper.max_retries_blocked,
            "http_403": settings.scraper.max_retries_blocked,
            "http_429": settings.scraper.max_retries_blocked,
            "timeout": settings.scraper.max_retries_transient,
            "connection_reset": settings.scraper.max_retries_transient,
            "http_5xx": settings.scraper.max_retries_transient,
        }

        self._robots_cache: dict[str, RobotFileParser] = {}
        self._headers = get_default_headers(self.user_agent)
        self.failure_reasons: Counter = Counter()
        # URLs skipped mid-run because their domain was in cooldown.
        # Exposed so the pipeline can keep them as ``pending`` instead of
        # marking them ``failed`` (which would consume retry budget).
        self.deferred_urls: set[str] = set()

        # --- Camoufox fallback (primary browser fallback) ---
        self.camoufox_enabled = settings.scraper.camoufox_enabled
        self._camoufox: CamoufoxScraper | None = None
        self._camoufox_settings: dict[str, Any] = {
            "timeout_ms": settings.scraper.camoufox_timeout_ms,
            "max_concurrent": settings.scraper.camoufox_max_concurrent,
        }
        self.camoufox_counts: Counter = Counter()

        # --- HttpxScraper (primary async fetcher) ---
        # Created per-batch inside _scrape_batch_async so the AsyncClient
        # binds to the running event loop. None outside of an active batch.
        self._httpx_scraper: HttpxScraper | None = None

        # --- Playwright fallback (legacy; Camoufox preferred) ---
        self.playwright_enabled = settings.scraper.playwright_enabled
        self._browser_targets: frozenset[str] = (
            settings.scraper.playwright_targeted_domain_set()
            if self.playwright_enabled
            else frozenset()
        )
        # Tests and callers can inject a pre-built BrowserScraper (mock);
        # otherwise one is created per async batch (inside
        # ``_scrape_batch_async``) so its asyncio primitives bind to the
        # running event loop.
        self._injected_browser = browser
        self.browser: BrowserScraper | None = browser
        self._browser_settings: dict[str, Any] = {
            "browser_type": settings.scraper.playwright_browser,
            "timeout_ms": settings.scraper.playwright_timeout_ms,
            "max_concurrent": settings.scraper.playwright_max_concurrent,
            "headed": settings.scraper.playwright_headed,
            "default_user_agent": settings.scraper.playwright_default_firefox_ua,
            "locale": settings.scraper.playwright_locale,
            "timezone_id": settings.scraper.playwright_timezone,
            "warmup_enabled": settings.scraper.playwright_warmup_enabled,
            "warmup_wait_ms_min": settings.scraper.playwright_warmup_wait_ms_min,
            "warmup_wait_ms_max": settings.scraper.playwright_warmup_wait_ms_max,
        }
        self.browser_counts: Counter = Counter()

        self.client = httpx.Client(
            headers=self._headers,
            timeout=self.timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self) -> "Scraper":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------- sync API

    def scrape_url(
        self,
        url: str,
        source_feed: str,
        feed_published_at: datetime | None = None,
    ) -> RawArticle | None:
        """Scrape a single URL synchronously.

        Respects robots.txt, circuit breaker, and rate limiting.
        Resolves redirect URLs before checking robots.txt so that
        intermediate redirect domains don't block scraping.
        """
        resolved = self._resolve_redirect_url(url)

        if not self.cb.is_allowed(resolved):
            logger.info("Circuit open for {url}, skipping", url=resolved)
            self.failure_reasons["circuit_breaker"] += 1
            return None

        if self.respect_robots and not self._is_allowed_by_robots(resolved):
            logger.info("Blocked by robots.txt: {url}", url=resolved)
            self.failure_reasons["robots_txt"] += 1
            return None

        if not self.cooldown.is_cool(resolved):
            logger.info("Domain cooldown active, skipping {url}", url=resolved)
            self.failure_reasons["cooldown_skip"] += 1
            self.deferred_urls.add(resolved)
            return None

        self._delay()

        try:
            article = self._fetch_and_parse(resolved, source_feed, feed_published_at)
            if article:
                self.cb.record_success(resolved)
                self.cooldown.record_success(resolved)
            else:
                self.failure_reasons["parse_error"] += 1
            return article
        except httpx.HTTPStatusError as e:
            self.cb.record_failure(resolved)
            self.failure_reasons[f"http_{e.response.status_code}"] += 1
            logger.warning(
                "HTTP {status} for {url}: {error}",
                status=e.response.status_code,
                url=resolved,
                error=str(e),
            )
            return None
        except httpx.TimeoutException:
            self.cb.record_failure(resolved)
            self.failure_reasons["timeout"] += 1
            logger.warning("Timeout for {url}", url=resolved)
            return None
        except Exception as e:
            self.cb.record_failure(resolved)
            self.failure_reasons["other"] += 1
            logger.error(
                "Scrape failed for {url}: {error}",
                url=resolved,
                error=str(e),
            )
            return None

    # ------------------------------------------------------------ batch API

    def scrape_batch(
        self,
        urls: list[dict],
    ) -> list[RawArticle]:
        """Scrape a batch of URLs using async concurrency."""
        if not urls:
            return []

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            return self._scrape_batch_sync(urls)

        return asyncio.run(self._scrape_batch_async(urls))

    def _log_failure_summary(self, total: int, success: int, mode: str) -> None:
        failed = total - success
        if failed > 0 and self.failure_reasons:
            breakdown = ", ".join(
                f"{count}x {reason}"
                for reason, count in self.failure_reasons.most_common()
            )
            logger.warning(
                "Batch ({mode}): {failed}/{total} URLs failed — {breakdown}",
                mode=mode,
                failed=failed,
                total=total,
                breakdown=breakdown,
            )

    def _scrape_batch_sync(self, urls: list[dict]) -> list[RawArticle]:
        articles: list[RawArticle] = []
        for item in urls:
            article = self.scrape_url(
                item["url"], item["source_feed"], item.get("published_at")
            )
            if article:
                articles.append(article)

        logger.info(
            "Batch complete (sync): {success}/{total} articles extracted",
            success=len(articles),
            total=len(urls),
        )
        self._log_failure_summary(len(urls), len(articles), "sync")
        return articles

    async def _scrape_batch_async(self, urls: list[dict]) -> list[RawArticle]:
        domain_sems: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(2)
        )
        global_sem = asyncio.Semaphore(self.max_concurrent)

        # Lazily construct browser scrapers inside the running event loop
        # so their asyncio primitives (Lock, Semaphore) bind to it.
        owned_httpx = HttpxScraper()
        self._httpx_scraper = owned_httpx

        owned_camoufox: CamoufoxScraper | None = None
        if self.camoufox_enabled:
            owned_camoufox = CamoufoxScraper(**self._camoufox_settings)
            self._camoufox = owned_camoufox

        owned_browser: BrowserScraper | None = None
        if self.playwright_enabled and self._injected_browser is None:
            owned_browser = BrowserScraper(**self._browser_settings)
            self.browser = owned_browser

        try:
            tasks = [
                self._async_scrape_one(
                    item["url"],
                    item["source_feed"],
                    domain_sems,
                    global_sem,
                    item.get("published_at"),
                )
                for item in urls
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await owned_httpx.close()
            self._httpx_scraper = None
            if owned_camoufox is not None:
                await owned_camoufox.close()
                self._camoufox = None
            if owned_browser is not None:
                await owned_browser.close()
                self.browser = self._injected_browser

        articles = [r for r in results if isinstance(r, RawArticle)]
        for r in results:
            if isinstance(r, Exception):
                self.failure_reasons["unhandled_exception"] += 1

        logger.info(
            "Batch complete (async): {success}/{total} articles extracted",
            success=len(articles),
            total=len(urls),
        )
        self._log_failure_summary(len(urls), len(articles), "async")
        return articles

    async def _async_scrape_one(
        self,
        url: str,
        source_feed: str,
        domain_sems: dict[str, asyncio.Semaphore],
        global_sem: asyncio.Semaphore,
        feed_published_at: datetime | None = None,
    ) -> RawArticle | None:
        """Scrape a single URL asynchronously with diagnostics + cooldown."""
        resolved = self._resolve_redirect_url(url)
        domain = urlparse(resolved).netloc

        if not self.cb.is_allowed(resolved):
            self.failure_reasons["circuit_breaker"] += 1
            self.diagnostics.record(
                DiagnosticRecord(
                    url=resolved,
                    domain=domain,
                    attempt=0,
                    stage="circuit_open",
                    error_type="circuit_open",
                )
            )
            return None

        if self.respect_robots and not self._is_allowed_by_robots(resolved):
            self.failure_reasons["robots_txt"] += 1
            self.diagnostics.record(
                DiagnosticRecord(
                    url=resolved,
                    domain=domain,
                    attempt=0,
                    stage="robots_blocked",
                    error_type="robots_txt",
                )
            )
            return None

        if not self.cooldown.is_cool(resolved):
            self.failure_reasons["cooldown_skip"] += 1
            self.deferred_urls.add(resolved)
            self.diagnostics.record(
                DiagnosticRecord(
                    url=resolved,
                    domain=domain,
                    attempt=0,
                    stage="cooldown_skip",
                    error_type="cooldown_skip",
                )
            )
            return None

        domain_sem = domain_sems[domain]

        async with global_sem, domain_sem:
            await self._apply_jitter_delay()
            ua = self.ua_rotator.for_domain(domain)

            budget_blocked = self._retry_overrides.get("blocked_bot", 1)
            budget_transient = self._retry_overrides.get("timeout", 3)
            max_attempts = max(budget_blocked, budget_transient)

            last_error_type: str | None = None
            for attempt in range(1, max_attempts + 1):
                if not self.cooldown.is_cool(resolved):
                    self.failure_reasons["cooldown_skip"] += 1
                    self.deferred_urls.add(resolved)
                    return None

                started = time.perf_counter()
                assert self._httpx_scraper is not None
                try:
                    result = await self._httpx_scraper.fetch(resolved, user_agent=ua)
                except httpx.TimeoutException as e:
                    last_error_type = "timeout"
                    self._record_transport_failure(
                        resolved, domain, attempt, ua, started, e
                    )
                    if attempt >= retry_budget(last_error_type, self._retry_overrides):
                        self.cb.record_failure(resolved)
                        self.failure_reasons["timeout"] += 1
                        return None
                    self.diagnostics.note_retry()
                    continue
                except httpx.HTTPError as e:
                    last_error_type = "connection_reset"
                    self._record_transport_failure(
                        resolved, domain, attempt, ua, started, e
                    )
                    if attempt >= retry_budget(last_error_type, self._retry_overrides):
                        self.cb.record_failure(resolved)
                        self.failure_reasons["connection_reset"] += 1
                        return None
                    self.diagnostics.note_retry()
                    continue

                latency_ms = int((time.perf_counter() - started) * 1000)
                status = result.status_code
                body_sample = (
                    result.html[: BODY_SAMPLE_BYTES * 2] if result.html else ""
                )
                signals = (
                    detect(status, result.headers, body_sample)
                    if self.block_detection_enabled
                    else BlockSignals()
                )

                # Non-2xx handling.
                if status >= 400:
                    error_type = classify_failure(status, signals, 0)
                    self._emit_record(
                        url=resolved,
                        domain=domain,
                        attempt=attempt,
                        stage="fetch",
                        status=status,
                        final_url=result.final_url,
                        latency_ms=latency_ms,
                        result=result,
                        body_sample=body_sample,
                        signals=signals,
                        extractor=None,
                        extracted_chars=0,
                        success=False,
                        error_type=error_type,
                        ua=ua,
                    )
                    self.failure_reasons[error_type] += 1
                    cls = error_class_for(error_type)
                    if cls in ("bot_block", "rate_limit"):
                        self.cooldown.record_block(
                            resolved, error_type, error_class=cls
                        )
                    if attempt >= retry_budget(error_type, self._retry_overrides):
                        self.cb.record_failure(resolved)
                        if cls == "bot_block":
                            article = await self._try_browser_fallbacks(
                                resolved, domain, source_feed, feed_published_at, ua=ua
                            )
                            if article is not None:
                                return article
                        return None
                    self.diagnostics.note_retry()
                    continue

                content_type = result.headers.get("content-type", "")
                # XML/RSS content is never escalated to Camoufox — the browser
                # would open a download dialog for XML (Page.goto: Download is
                # starting). The non_html path returns cleanly without fallback.
                if "text/html" not in content_type:
                    self._emit_record(
                        url=resolved,
                        domain=domain,
                        attempt=attempt,
                        stage="fetch",
                        status=status,
                        final_url=result.final_url,
                        latency_ms=latency_ms,
                        result=result,
                        body_sample=body_sample,
                        signals=signals,
                        extractor=None,
                        extracted_chars=0,
                        success=False,
                        error_type="non_html",
                        ua=ua,
                    )
                    self.failure_reasons["non_html"] += 1
                    return None

                if signals.blocked:
                    self._emit_record(
                        url=resolved,
                        domain=domain,
                        attempt=attempt,
                        stage="fetch",
                        status=status,
                        final_url=result.final_url,
                        latency_ms=latency_ms,
                        result=result,
                        body_sample=body_sample,
                        signals=signals,
                        extractor=None,
                        extracted_chars=0,
                        success=False,
                        error_type="blocked_bot",
                        ua=ua,
                    )
                    self.failure_reasons["blocked_bot"] += 1
                    self.cooldown.record_block(
                        resolved, "blocked_bot", error_class="bot_block"
                    )
                    self.cb.record_failure(resolved)
                    article = await self._try_browser_fallbacks(
                        resolved, domain, source_feed, feed_published_at, ua=ua
                    )
                    if article is not None:
                        return article
                    return None

                # Parse stage.
                article = self.parser.parse(
                    result.html,
                    result.final_url,
                    source_feed,
                    feed_published_at,
                )
                extracted_chars = len(article.content) if article else 0
                success = article is not None
                extractor = "primary"

                self._emit_record(
                    url=resolved,
                    domain=domain,
                    attempt=attempt,
                    stage="parse",
                    status=status,
                    final_url=result.final_url,
                    latency_ms=latency_ms,
                    result=result,
                    body_sample=body_sample,
                    signals=signals,
                    extractor=extractor,
                    extracted_chars=extracted_chars,
                    success=success,
                    error_type=(
                        None
                        if success
                        else classify_failure(status, signals, extracted_chars)
                    ),
                    ua=ua,
                )

                if success:
                    self.cb.record_success(resolved)
                    self.cooldown.record_success(resolved)
                    return article

                self.failure_reasons["parse_error"] += 1
                parse_error_type = classify_failure(status, signals, extracted_chars)

                # Escalate to browser fallback before parking the domain.
                # parser_hard cooldown only applies after ALL layers fail —
                # Camoufox can handle sites where httpx gets real HTML but the
                # template changed enough that our CSS selectors miss.
                if parse_error_type == "selector_missing":
                    browser_article = await self._try_browser_fallbacks(
                        resolved, domain, source_feed, feed_published_at, ua=ua
                    )
                    if browser_article is not None:
                        return browser_article

                self.cooldown.record_block(
                    resolved,
                    parse_error_type,
                    error_class=error_class_for(parse_error_type),
                )
                return None

            return None

    # -------------------------------------------------- browser fallbacks

    async def _try_browser_fallbacks(
        self,
        url: str,
        domain: str,
        source_feed: str,
        feed_published_at: datetime | None,
        *,
        ua: str,
    ) -> "RawArticle | None":
        """Try Camoufox then Playwright in order. Return first success."""
        if self._should_try_camoufox():
            article = await self._camoufox_fallback(
                url, domain, source_feed, feed_published_at, ua=ua
            )
            if article is not None:
                return article
        if self._should_try_browser(domain):
            article = await self._browser_fallback(
                url, domain, source_feed, feed_published_at, ua=ua
            )
            if article is not None:
                return article
        return None

    def _should_try_camoufox(self) -> bool:
        return self.camoufox_enabled and self._camoufox is not None

    def _should_try_browser(self, domain: str) -> bool:
        """True if the Playwright fallback should run for ``domain``."""
        return (
            self.playwright_enabled
            and self.browser is not None
            and domain in self._browser_targets
        )

    async def _browser_fallback(
        self,
        url: str,
        domain: str,
        source_feed: str,
        feed_published_at: datetime | None,
        *,
        ua: str,
    ) -> RawArticle | None:
        """Render ``url`` with Playwright and try to parse the result.

        This is the last-chance path for the handful of RN portals that
        serve Cloudflare JS challenges / WAF walls to our egress. It
        reuses the existing block detector and ArticleParser so a
        successful render follows the same shape as an httpx success.
        """
        assert self.browser is not None
        self.browser_counts["attempts"] += 1
        result = await self.browser.fetch(url, user_agent=ua)
        if result is None:
            # Playwright unavailable — treated as disabled, no counter
            # movement beyond the attempt.
            self.browser_counts["failed"] += 1
            return None

        if result.error or not result.html:
            self.browser_counts["failed"] += 1
            self.diagnostics.record(
                DiagnosticRecord(
                    url=url,
                    domain=domain,
                    attempt=0,
                    stage="browser_render",
                    status_code=result.status_code or None,
                    final_url=result.final_url,
                    error_type="browser_failed",
                    error_detail=result.error,
                    user_agent=ua,
                )
            )
            return None

        body_sample = result.html[: BODY_SAMPLE_BYTES * 2]
        signals = detect(result.status_code, result.headers, body_sample)
        signals.browser_required = True

        if signals.blocked:
            # Even the browser saw a challenge — bail out. Do not park
            # the domain again; the httpx path already recorded it.
            self.browser_counts["failed"] += 1
            self.diagnostics.record(
                DiagnosticRecord(
                    url=url,
                    domain=domain,
                    attempt=0,
                    stage="browser_render",
                    status_code=result.status_code,
                    final_url=result.final_url,
                    headers=filter_headers(result.headers),
                    body_sample_hash=hash_body_sample(body_sample),
                    block_signals=signals.to_dict(),
                    error_type="blocked_bot",
                    user_agent=ua,
                )
            )
            return None

        article = self.parser.parse(
            result.html,
            result.final_url,
            source_feed,
            feed_published_at,
        )
        extracted_chars = len(article.content) if article else 0
        self.diagnostics.record(
            DiagnosticRecord(
                url=url,
                domain=domain,
                attempt=0,
                stage="browser_render",
                status_code=result.status_code,
                final_url=result.final_url,
                headers=filter_headers(result.headers),
                body_sample_hash=hash_body_sample(body_sample),
                block_signals=signals.to_dict(),
                extractor_used="playwright_render",
                extracted_chars=extracted_chars,
                extraction_success=article is not None,
                error_type=(
                    None
                    if article is not None
                    else classify_failure(result.status_code, signals, extracted_chars)
                ),
                user_agent=ua,
            )
        )
        if article is None:
            self.browser_counts["failed"] += 1
            return None

        self.browser_counts["success"] += 1
        self.cb.record_success(url)
        self.cooldown.record_success(url)
        self.diagnostics.note_parser_recovery(domain, "playwright_render")
        logger.info(
            "browser_render recovered {url} (domain={domain})",
            url=url,
            domain=domain,
        )
        return article

    async def _camoufox_fallback(
        self,
        url: str,
        domain: str,
        source_feed: str,
        feed_published_at: datetime | None,
        *,
        ua: str,
    ) -> "RawArticle | None":
        """Render ``url`` with Camoufox and try to parse the result."""
        assert self._camoufox is not None
        self.camoufox_counts["attempts"] += 1
        result = await self._camoufox.fetch(url, user_agent=ua)
        if result is None:
            self.camoufox_counts["failed"] += 1
            return None

        if result.error or not result.html:
            self.camoufox_counts["failed"] += 1
            self.diagnostics.record(
                DiagnosticRecord(
                    url=url,
                    domain=domain,
                    attempt=0,
                    stage="camoufox_render",
                    status_code=result.status_code or None,
                    final_url=result.final_url,
                    error_type="camoufox_failed",
                    error_detail=result.error,
                    user_agent=ua,
                )
            )
            return None

        body_sample = result.html[: BODY_SAMPLE_BYTES * 2]
        signals = detect(result.status_code, result.headers, body_sample)
        signals.browser_required = True

        if signals.blocked:
            self.camoufox_counts["failed"] += 1
            self.diagnostics.record(
                DiagnosticRecord(
                    url=url,
                    domain=domain,
                    attempt=0,
                    stage="camoufox_render",
                    status_code=result.status_code,
                    final_url=result.final_url,
                    headers=filter_headers(result.headers),
                    body_sample_hash=hash_body_sample(body_sample),
                    block_signals=signals.to_dict(),
                    error_type="blocked_bot",
                    user_agent=ua,
                )
            )
            return None

        article = self.parser.parse(
            result.html,
            result.final_url,
            source_feed,
            feed_published_at,
        )
        extracted_chars = len(article.content) if article else 0
        self.diagnostics.record(
            DiagnosticRecord(
                url=url,
                domain=domain,
                attempt=0,
                stage="camoufox_render",
                status_code=result.status_code,
                final_url=result.final_url,
                headers=filter_headers(result.headers),
                body_sample_hash=hash_body_sample(body_sample),
                block_signals=signals.to_dict(),
                extractor_used="camoufox_render",
                extracted_chars=extracted_chars,
                extraction_success=article is not None,
                error_type=(
                    None
                    if article is not None
                    else classify_failure(result.status_code, signals, extracted_chars)
                ),
                user_agent=ua,
            )
        )
        if article is None:
            self.camoufox_counts["failed"] += 1
            return None

        self.camoufox_counts["success"] += 1
        self.cb.record_success(url)
        self.cooldown.record_success(url)
        self.diagnostics.note_parser_recovery(domain, "camoufox_render")
        logger.info(
            "camoufox_render recovered {url} (domain={domain})",
            url=url,
            domain=domain,
        )
        return article

    # ------------------------------------------------------------ helpers

    def _record_transport_failure(
        self,
        url: str,
        domain: str,
        attempt: int,
        ua: str,
        started: float,
        exc: Exception,
    ) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        error_type = classify_failure(
            None, BlockSignals(), 0, exception_name=type(exc).__name__
        )
        self.diagnostics.record(
            DiagnosticRecord(
                url=url,
                domain=domain,
                attempt=attempt,
                stage="fetch",
                status_code=None,
                final_url=None,
                latency_ms=latency_ms,
                content_length_bytes=None,
                body_sample_hash=None,
                headers={},
                block_signals=BlockSignals().to_dict(),
                extractor_used=None,
                extracted_chars=0,
                extraction_success=False,
                error_type=error_type,
                error_detail=str(exc)[:200],
                user_agent=ua,
            )
        )

    def _emit_record(
        self,
        *,
        url: str,
        domain: str,
        attempt: int,
        stage: str,
        status: int | None,
        final_url: str | None,
        latency_ms: int | None,
        result: BrowserFetchResult | None,
        body_sample: str,
        signals: BlockSignals,
        extractor: str | None,
        extracted_chars: int,
        success: bool,
        error_type: str | None,
        ua: str,
    ) -> None:
        content_length = None
        headers_out: dict[str, str] = {}
        if result is not None:
            headers_out = filter_headers(result.headers)
            try:
                content_length = int(result.headers.get("content-length", "0")) or None
            except ValueError:
                content_length = None
            if content_length is None and result.html:
                content_length = len(result.html.encode("utf-8", errors="ignore"))

        self.diagnostics.record(
            DiagnosticRecord(
                url=url,
                domain=domain,
                attempt=attempt,
                stage=stage,
                status_code=status,
                final_url=final_url,
                latency_ms=latency_ms,
                content_length_bytes=content_length,
                body_sample_hash=hash_body_sample(body_sample),
                headers=headers_out,
                block_signals=signals.to_dict(),
                extractor_used=extractor,
                extracted_chars=extracted_chars,
                extraction_success=success,
                error_type=error_type,
                user_agent=ua,
            )
        )

    async def _apply_jitter_delay(self) -> None:
        base = random.uniform(self.delay_min, self.delay_max)
        if self.jitter_max_ms > 0:
            extra = random.uniform(self.jitter_min_ms, self.jitter_max_ms) / 1000.0
            base += extra
        await asyncio.sleep(base)

    @retry_on_network_error(max_attempts=2, min_wait=2.0)
    def _fetch_and_parse(
        self,
        url: str,
        source_feed: str,
        feed_published_at: datetime | None = None,
    ) -> RawArticle | None:
        response = self.client.get(url)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            logger.debug("Non-HTML content at {url}: {ct}", url=url, ct=content_type)
            return None

        return self.parser.parse(response.text, url, source_feed, feed_published_at)

    @staticmethod
    def _resolve_redirect_url(url: str) -> str:
        for marker in ("/*https://", "/*http://"):
            idx = url.find(marker)
            if idx != -1:
                return url[idx + 2 :]
        return url

    def _delay(self) -> None:
        delay = random.uniform(self.delay_min, self.delay_max)
        time.sleep(delay)

    def _is_allowed_by_robots(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        if base not in self._robots_cache:
            rp = RobotFileParser()
            rp.set_url(f"{base}/robots.txt")
            try:
                resp = self.client.get(f"{base}/robots.txt")
                if resp.status_code != 200:
                    return True
                lines = []
                for line in resp.text.splitlines():
                    stripped = line.split("#")[0].strip()
                    if stripped.lower().startswith(("disallow:", "allow:")):
                        _, _, path = stripped.partition(":")
                        path = path.strip()
                        if path and not path.startswith("/"):
                            continue
                    lines.append(line)
                rp.parse(lines)
            except Exception:
                return True
            self._robots_cache[base] = rp

        return self._robots_cache[base].can_fetch(self.user_agent, url)
