"""Async httpx article fetcher with Firefox-mimicking headers.

Primary scraper for all domains — including CF-CDN-protected sites, where
plain httpx bypasses managed-challenge enforcement (CF cannot run JS without
a browser to execute it).

Returns ``BrowserFetchResult`` so it is drop-in interchangeable with
``CamoufoxScraper`` as inputs to the block detector and ``ArticleParser``.
Failures raise rather than returning ``None``; the orchestrating ``Scraper``
handles retry / fallback logic.
"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger

from mapear_rss.extraction.browser_scraper import BrowserFetchResult

# 2 total attempts: 1 original + 1 retry with fixed 2-second backoff.
_MAX_FETCH_ATTEMPTS = 2
_RETRY_DELAY_S = 2.0
_RETRYABLE = (httpx.TimeoutException, httpx.ConnectError)

# Firefox 134 desktop on Linux — matches the CF-bypass tests in bench_scraper.
_FIREFOX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0"

# Accept-Encoding deliberately omits "br": production Cloud Run images may not
# have brotli installed. gzip + deflate cover all real-world servers.
_BASE_HEADERS: dict[str, str] = {
    "User-Agent": _FIREFOX_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class HttpxScraper:
    """Async httpx wrapper for article fetching.

    Manages a single ``AsyncClient`` across fetches (connection pooling).
    Call ``close()`` / use as ``async with`` to release the client when done.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        headers=_BASE_HEADERS,
                        follow_redirects=True,
                        # Split timeout: 15 s to establish connection, 30 s to read.
                        timeout=httpx.Timeout(
                            connect=15.0, read=30.0, write=10.0, pool=5.0
                        ),
                        max_redirects=5,
                    )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> HttpxScraper:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def fetch(
        self,
        url: str,
        *,
        user_agent: str | None = None,
    ) -> BrowserFetchResult:
        """Fetch ``url`` and return its HTML.

        Retries once (2 total attempts) on TimeoutException / ConnectError with
        a fixed 2-second delay. All other exceptions propagate immediately.
        Callers handle the full exception set.
        """
        client = await self._get_client()
        extra: dict[str, str] = {}
        if user_agent:
            extra["User-Agent"] = user_agent

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_FETCH_ATTEMPTS + 1):
            try:
                logger.debug(
                    "httpx_scraper: GET {url} (attempt {a})", url=url, a=attempt
                )
                resp = await client.get(url, headers=extra or None)
                return BrowserFetchResult(
                    html=resp.text,
                    status_code=resp.status_code,
                    final_url=str(resp.url),
                    headers=dict(resp.headers),
                )
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt < _MAX_FETCH_ATTEMPTS:
                    logger.debug(
                        "httpx_scraper: transient {exc}, retrying in {d}s",
                        exc=type(exc).__name__,
                        d=_RETRY_DELAY_S,
                    )
                    await asyncio.sleep(_RETRY_DELAY_S)

        raise last_exc  # type: ignore[misc]
