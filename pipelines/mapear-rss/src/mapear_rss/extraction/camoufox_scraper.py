"""Camoufox-based browser fallback for sites that genuinely require JS rendering.

Camoufox is a patched Firefox build that removes every browser-automation
indicator (navigator.webdriver, CDP fingerprint, font/canvas metrics) that
Cloudflare Bot Management and DataDome key on. The API is Playwright-
compatible, so ``BrowserFetchResult`` flows into the same block detector and
``ArticleParser`` pipeline as httpx responses.

This module is **opt-in**: nothing imports Camoufox at module load time.
Set ``SCRAPER_CAMOUFOX_ENABLED=true`` and install the package:

    pip install "camoufox[geoip]" && python -m camoufox fetch

Design mirrors ``BrowserScraper``:
- Browser launched lazily on first ``fetch()`` call.
- One shared browser instance; concurrent contexts gated by a semaphore.
- ``close()`` / ``async with`` releases the subprocess.
- Returns ``None`` when Camoufox is not installed so callers fall through
  gracefully.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from loguru import logger

from mapear_rss.extraction.browser_scraper import BrowserFetchResult

if TYPE_CHECKING:  # pragma: no cover
    pass


class CamoufoxScraper:
    """Lazy-initialized Camoufox wrapper.

    The browser is launched on the first ``fetch()`` call. Call ``close()``
    (or use ``async with``) to release the subprocess at the end of a batch.
    """

    def __init__(
        self,
        *,
        timeout_ms: int = 20000,
        max_concurrent: int = 2,
        locale: str = "pt-BR",
        timezone_id: str = "America/Fortaleza",
    ) -> None:
        self.timeout_ms = timeout_ms
        self.locale = locale
        self.timezone_id = timezone_id
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._lock = asyncio.Lock()
        self._cm: Any = None
        self._browser: Any = None
        self._unavailable: bool = False

    async def __aenter__(self) -> CamoufoxScraper:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _ensure_browser(self) -> bool:
        """Lazy-start Camoufox. Return False if unavailable."""
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
                from camoufox.async_api import AsyncCamoufox
            except ImportError:
                logger.warning(
                    "camoufox_scraper: camoufox package not installed — "
                    "run 'pip install camoufox[geoip] && python -m camoufox fetch'. "
                    "Camoufox fallback disabled."
                )
                self._unavailable = True
                return False

            try:
                self._cm = AsyncCamoufox(headless=True, os="linux")
                self._browser = await self._cm.__aenter__()
                logger.info(
                    "camoufox_scraper: browser launched " "(locale={locale}, tz={tz})",
                    locale=self.locale,
                    tz=self.timezone_id,
                )
                return True
            except Exception as exc:
                logger.warning(
                    "camoufox_scraper: failed to launch browser: {err}. "
                    "Fallback disabled for this run.",
                    err=str(exc),
                )
                self._unavailable = True
                return False

    async def fetch(
        self,
        url: str,
        *,
        user_agent: str | None = None,
    ) -> BrowserFetchResult | None:
        """Render ``url`` and return its final HTML.

        Returns ``None`` when Camoufox is unavailable so the caller can
        fall through to the existing failure path.
        """
        if not await self._ensure_browser():
            return None

        assert self._browser is not None

        async with self._sem:
            ctx = None
            try:
                ctx = await self._browser.new_context(
                    locale=self.locale,
                    timezone_id=self.timezone_id,
                    viewport={"width": 1920, "height": 1080},
                )
                if user_agent:
                    await ctx.set_extra_http_headers({"User-Agent": user_agent})
                page = await ctx.new_page()

                resp = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                status = resp.status if resp else 0
                final_url = page.url
                html = await page.content()
                headers = dict(resp.headers) if resp else {}

                return BrowserFetchResult(
                    html=html,
                    status_code=status,
                    final_url=final_url,
                    headers=headers,
                )
            except Exception as exc:
                logger.debug(
                    "camoufox_scraper: fetch failed for {url}: {err}",
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
                if ctx is not None:
                    with contextlib.suppress(Exception):
                        await ctx.close()

    async def close(self) -> None:
        if self._cm is not None:
            with contextlib.suppress(Exception):
                await self._cm.__aexit__(None, None, None)
            self._cm = None
            self._browser = None
