"""Headless-render fallback for the handful of domains that serve
Cloudflare JS challenges / WAF walls to our httpx egress.

This module is **opt-in**: nothing imports Playwright at module load
time. Production environments that enable ``SCRAPER_PLAYWRIGHT_ENABLED``
must have installed the ``browser`` poetry group and run
``playwright install firefox --with-deps`` once.

Design goals:

1. **Selective** — only targeted domains hit the browser path, and only
   after the httpx retries for ``bot_block`` have been exhausted.
2. **Bounded cost** — one shared Browser instance, N concurrent contexts
   gated by a semaphore. Rendering is 3–5 s and ~80 MB of RSS per
   context, so the default cap is 2.
3. **Composable with existing telemetry** — fetch() returns the same
   raw shape the httpx path produces (html, status_code, headers) so
   ``block_detector.detect`` and ``ArticleParser`` can be reused as-is.
4. **Fail-quiet** — when Playwright is not installed or the browser
   launch fails, the fallback is a no-op and the scraper proceeds as
   if it had been disabled.

Camada 1 stealth reinforcements (no library swap):

- Firefox user prefs flip the JS-visible webdriver flag off and disable
  WebRTC so local-IP leaks don't betray us.
- Context sets locale=pt-BR, timezone=America/Fortaleza, viewport
  1920x1080. Locale/timezone must match the UA's implied geography —
  Cloudflare's bot-score rules key on this mismatch.
- The UA is forced to a current Firefox desktop UA when the caller
  passes a Chrome/Safari UA but the launcher is Firefox, to avoid
  trivial UA/engine fingerprint mismatches.
- Storage_state (cookies + localStorage) is persisted per domain across
  fetches within a run, so the second fetch to a CF-behind domain
  reuses the ``__cf_bm`` it got on the first.
- Warm-up: the first fetch to a domain visits its home, idles 2-4 s,
  and only then navigates to the article URL. Kept per-domain so we
  pay it at most once per run.
- ``headed=True`` (with Xvfb) is opt-in via SCRAPER_PLAYWRIGHT_HEADED
  for environments that can afford a display — this removes the
  "headless mode" fingerprint that is still the single biggest tell on
  Linux.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Browser, Playwright


DEFAULT_FIREFOX_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0"
)

# firefox_user_prefs applied at launch-time. Limited to prefs that:
# (a) flip off the most obvious webdriver tells,
# (b) close fingerprint-leak channels (WebRTC),
# (c) pick pt-BR at the network layer so Accept-Language is consistent
#     with our context locale.
# We deliberately do NOT set ``general.useragent.override`` here — UA is
# set per-context via ``user_agent=``, and setting it in two places
# creates a Playwright/Firefox inconsistency window on startup.
FIREFOX_STEALTH_PREFS: dict[str, Any] = {
    # The biggest and cheapest tell: flip the JS-visible flag.
    "dom.webdriver.enabled": False,
    # Playwright used to toggle this; leaving it off is belt-and-braces.
    "useAutomationExtension": False,
    # WebRTC leaks local/private IPs even when behind a proxy. Off.
    "media.peerconnection.enabled": False,
    # Honor pt-BR first in Accept-Language, matching our context locale.
    "intl.accept_languages": "pt-BR, pt, en-US, en",
    # Match a defaults-accepted profile: a real user rarely flips these.
    "network.cookie.cookieBehavior": 0,
    "privacy.trackingprotection.enabled": False,
    # Reduce "suspiciously clean" noise — a real browser has had these
    # on at least once.
    "browser.cache.disk.enable": True,
    "browser.cache.memory.enable": True,
}


def _coerce_firefox_ua(requested_ua: str | None, fallback: str) -> str:
    """Return a UA that matches Firefox engine expectations.

    If the caller passes a Chrome/Safari UA into a Firefox launcher,
    ``navigator.userAgent`` says Chrome but the TLS/JS engine fingerprint
    screams Firefox — that mismatch is exactly the kind of signal
    Cloudflare's bot score looks for. Silently substitute when needed.
    """
    if not requested_ua:
        return fallback
    if "Firefox/" in requested_ua and "Gecko/" in requested_ua:
        return requested_ua
    logger.debug(
        "browser_scraper: caller UA does not match Firefox engine — "
        "substituting default ({requested} -> {fallback})",
        requested=requested_ua,
        fallback=fallback,
    )
    return fallback


def _sec_ch_ua_headers(user_agent: str | None) -> dict[str, str]:
    """Build Sec-CH-UA client-hint headers that match the given User-Agent.

    Only Chrome/Edge send these headers natively. Firefox omits them, so we
    return an empty dict for non-Chromium UAs to avoid fingerprint mismatch.
    """
    if not user_agent or "Chrome/" not in user_agent:
        return {}
    m = re.search(r"Chrome/(\d+)", user_agent)
    ver = m.group(1) if m else "131"
    if "Edg/" in user_agent:
        brand = "Microsoft Edge"
        sec = f'"{brand}";v="{ver}", "Chromium";v="{ver}", "Not=A?Brand";v="99"'
    else:
        brand = "Google Chrome"
        sec = f'"{brand}";v="{ver}", "Chromium";v="{ver}", "Not=A?Brand";v="99"'
    return {
        "sec-ch-ua": sec,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


async def _apply_stealth(page: Any) -> None:
    """Apply playwright-stealth evasion patches if the package is installed.

    Supports both playwright-stealth 1.x (stealth_async) and 2.x (Stealth class).
    """
    try:
        # playwright-stealth >= 2.x
        from playwright_stealth import Stealth  # type: ignore[import-untyped]

        await Stealth().apply_stealth_async(page)
    except ImportError:
        pass
    except AttributeError:
        # Fallback for 1.x API
        try:
            from playwright_stealth import stealth_async  # type: ignore[import-untyped]

            await stealth_async(page)
        except ImportError:
            pass


@dataclass
class BrowserFetchResult:
    """Return shape for ``BrowserScraper.fetch``.

    Mirrors the subset of httpx.Response that the rest of the scraper
    relies on, so the downstream code path does not branch on the
    fallback origin.
    """

    html: str
    status_code: int
    final_url: str
    headers: dict[str, str]
    error: str | None = None


class BrowserScraper:
    """Lazy-initialized Playwright wrapper.

    The browser is launched on the first ``fetch()`` call, not in
    ``__init__`` — so scrapers that never need the fallback pay zero
    cost. Call ``close()`` (or use ``async with``) to release the
    Playwright subprocess at the end of a batch.
    """

    def __init__(
        self,
        *,
        browser_type: str = "firefox",
        timeout_ms: int = 20000,
        max_concurrent: int = 2,
        headed: bool = False,
        default_user_agent: str = DEFAULT_FIREFOX_UA,
        locale: str = "pt-BR",
        timezone_id: str = "America/Fortaleza",
        warmup_enabled: bool = True,
        warmup_wait_ms_min: int = 2000,
        warmup_wait_ms_max: int = 4000,
    ) -> None:
        self.browser_type = browser_type
        self.timeout_ms = timeout_ms
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._unavailable: bool = False
        self._headed = headed
        self.default_user_agent = default_user_agent
        self.locale = locale
        self.timezone_id = timezone_id
        self.warmup_enabled = warmup_enabled
        self.warmup_wait_ms_min = warmup_wait_ms_min
        self.warmup_wait_ms_max = warmup_wait_ms_max

        # Per-domain state. Keys are bare netlocs (``example.com``).
        self._storage_by_domain: dict[str, dict] = {}
        self._warmed_domains: set[str] = set()
        # Serializes warm-up + storage_state writes for the same domain
        # so two concurrent fetches to the same host don't both warm up
        # and don't race on storage_state read/write.
        self._domain_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> BrowserScraper:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def _domain_lock(self, domain: str) -> asyncio.Lock:
        lock = self._domain_locks.get(domain)
        if lock is None:
            lock = asyncio.Lock()
            self._domain_locks[domain] = lock
        return lock

    async def _ensure_browser(self) -> bool:
        """Lazy-start Playwright. Return False if unavailable."""
        if self._browser is not None:
            return True
        if self._unavailable:
            return False

        async with self._lock:
            if self._browser is not None:
                return True
            if self._unavailable:
                return False
            try:
                # Lazy import so importing this module never requires
                # Playwright to be installed.
                from playwright.async_api import async_playwright
            except ImportError:
                logger.warning(
                    "browser_scraper: playwright package not installed — "
                    "install with 'poetry install --with browser' and "
                    "'playwright install firefox'. Fallback disabled."
                )
                self._unavailable = True
                return False

            try:
                self._playwright = await async_playwright().start()
                launcher = getattr(self._playwright, self.browser_type)

                launch_kwargs: dict[str, Any] = {
                    "headless": self._effective_headless(),
                }
                if self.browser_type == "firefox":
                    launch_kwargs["firefox_user_prefs"] = FIREFOX_STEALTH_PREFS

                self._browser = await launcher.launch(**launch_kwargs)
                logger.info(
                    "browser_scraper: launched {browser} "
                    "(headless={headless}, locale={locale}, tz={tz})",
                    browser=self.browser_type,
                    headless=launch_kwargs["headless"],
                    locale=self.locale,
                    tz=self.timezone_id,
                )
                return True
            except Exception as exc:
                logger.warning(
                    "browser_scraper: failed to launch {browser}: {err}. "
                    "Fallback disabled for this run.",
                    browser=self.browser_type,
                    err=str(exc),
                )
                self._unavailable = True
                return False

    def _effective_headless(self) -> bool:
        """Resolve headed vs headless accounting for missing $DISPLAY.

        On Linux, headed mode needs an X display (Xvfb or a real one).
        If the caller asked for headed but $DISPLAY is missing and we
        aren't on Windows/macOS, fall back to headless with a warning —
        crashing at launch would be worse than losing some stealth.
        """
        if not self._headed:
            return True
        if os.name == "posix" and os.environ.get("DISPLAY"):
            return False
        if os.name != "posix":
            return False
        logger.warning(
            "browser_scraper: headed requested but $DISPLAY is unset — "
            "falling back to headless. Start Xvfb (e.g. "
            "'xvfb-run -a ...') or unset SCRAPER_PLAYWRIGHT_HEADED."
        )
        return True

    async def fetch(
        self,
        url: str,
        *,
        user_agent: str | None = None,
    ) -> BrowserFetchResult | None:
        """Render ``url`` and return its final HTML.

        Returns ``None`` if Playwright is unavailable so the caller can
        fall through to the existing failure path.
        """
        if not await self._ensure_browser():
            return None

        assert self._browser is not None

        domain = urlparse(url).netloc
        effective_ua = (
            _coerce_firefox_ua(user_agent, self.default_user_agent)
            if self.browser_type == "firefox"
            else (user_agent or self.default_user_agent)
        )

        async with self._sem:
            # Serialize per-domain warm-up + storage_state mutations.
            # The HTTP fetch itself happens OUTSIDE this lock (once we
            # have the context), so concurrency across domains is
            # unaffected; only same-domain racers wait.
            async with self._domain_lock(domain):
                storage_state = self._storage_by_domain.get(domain)

            context = None
            try:
                context_kwargs: dict[str, Any] = {
                    "user_agent": effective_ua,
                    "locale": self.locale,
                    "timezone_id": self.timezone_id,
                    "viewport": {"width": 1920, "height": 1080},
                }
                if storage_state is not None:
                    context_kwargs["storage_state"] = storage_state

                context = await self._browser.new_context(**context_kwargs)
                extra_headers = {
                    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                    # Keep br on: the real Firefox sends it, and
                    # Playwright Firefox has Brotli support bundled.
                    "Accept-Encoding": "gzip, deflate, br",
                    **_sec_ch_ua_headers(effective_ua),
                }
                await context.set_extra_http_headers(extra_headers)
                page = await context.new_page()
                await _apply_stealth(page)

                await self._maybe_warmup(page, domain)

                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                status = response.status if response else 0
                final_url = page.url
                html = await page.content()
                headers = dict(response.headers) if response else {}

                # Persist the post-fetch cookie/localStorage state so
                # the next URL on the same host reuses __cf_bm/etc.
                try:
                    async with self._domain_lock(domain):
                        self._storage_by_domain[domain] = await context.storage_state()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "browser_scraper: storage_state snapshot failed "
                        "for {d}: {e}",
                        d=domain,
                        e=exc,
                    )

                return BrowserFetchResult(
                    html=html,
                    status_code=status,
                    final_url=final_url,
                    headers=headers,
                )
            except Exception as exc:
                logger.debug(
                    "browser_scraper: fetch failed for {url}: {err}",
                    url=url,
                    err=str(exc),
                )
                return BrowserFetchResult(
                    html="",
                    status_code=0,
                    final_url=url,
                    headers={},
                    error=type(exc).__name__,
                )
            finally:
                if context is not None:
                    with contextlib.suppress(Exception):
                        await context.close()

    async def _maybe_warmup(self, page: Any, domain: str) -> None:
        """Visit ``https://{domain}/`` and idle before the target URL.

        No-op when warm-up is disabled or the domain has already been
        warmed during this run. A failed warm-up (timeout, challenge on
        home) is swallowed: the main fetch will still run, and the
        upstream block detector decides what to do with the result.
        """
        if not self.warmup_enabled or not domain:
            return
        # Use the domain lock to prevent two concurrent fetches from
        # both warming up the same domain.
        async with self._domain_lock(domain):
            if domain in self._warmed_domains:
                return
            home_url = f"https://{domain}/"
            try:
                await page.goto(
                    home_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                wait_ms = random.uniform(
                    self.warmup_wait_ms_min, self.warmup_wait_ms_max
                )
                await page.wait_for_timeout(wait_ms)
                self._warmed_domains.add(domain)
                logger.debug(
                    "browser_scraper: warmed {domain} ({wait:.0f} ms)",
                    domain=domain,
                    wait=wait_ms,
                )
            except Exception as exc:
                # Mark warmed anyway — a second try is unlikely to
                # behave differently and would just burn budget.
                self._warmed_domains.add(domain)
                logger.debug(
                    "browser_scraper: warmup failed for {domain}: {err}",
                    domain=domain,
                    err=str(exc),
                )

    async def close(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None
