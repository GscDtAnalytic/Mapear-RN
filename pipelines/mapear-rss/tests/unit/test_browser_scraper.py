"""Unit tests for BrowserScraper.

These tests never touch a real browser. ``async_playwright`` is mocked at
the module level so CI doesn't need Playwright installed.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from mapear_rss.extraction.browser_scraper import BrowserFetchResult, BrowserScraper


def _install_fake_playwright(monkeypatch, *, fetch_behavior):
    """Install a fake ``playwright.async_api`` module into ``sys.modules``.

    ``fetch_behavior`` is a callable invoked as ``behavior(page, url)`` and
    should set up the mock ``page`` (e.g. make ``goto`` return a response,
    raise, etc.) and return the ``response`` object that ``page.goto``
    should resolve to (or raise via ``side_effect``).
    """
    response = MagicMock()
    response.status = 200
    response.headers = {"server": "cloudflare"}

    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    page.content = AsyncMock(return_value="<html>rendered</html>")
    page.url = "https://example.com/final"

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    context.set_extra_http_headers = AsyncMock()

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    launcher = MagicMock()
    launcher.launch = AsyncMock(return_value=browser)

    playwright = MagicMock()
    playwright.firefox = launcher
    playwright.chromium = launcher
    playwright.stop = AsyncMock()

    started = MagicMock()
    started.start = AsyncMock(return_value=playwright)

    async_playwright = MagicMock(return_value=started)

    fake_module = ModuleType("playwright.async_api")
    fake_module.async_playwright = async_playwright  # type: ignore[attr-defined]
    parent = ModuleType("playwright")
    parent.async_api = fake_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "playwright", parent)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_module)

    fetch_behavior(page, response)
    return page, browser


@pytest.mark.asyncio
async def test_fetch_success(monkeypatch):
    def setup(page, response):
        page.goto = AsyncMock(return_value=response)
        page.content = AsyncMock(return_value="<html>OK</html>")

    _install_fake_playwright(monkeypatch, fetch_behavior=setup)

    scraper = BrowserScraper(browser_type="firefox")
    result = await scraper.fetch("https://example.com")
    await scraper.close()

    assert isinstance(result, BrowserFetchResult)
    assert result.html == "<html>OK</html>"
    assert result.status_code == 200
    assert result.error is None


@pytest.mark.asyncio
async def test_fetch_timeout_returns_error_result(monkeypatch):
    def setup(page, _response):
        page.goto = AsyncMock(side_effect=TimeoutError("nav timeout"))

    _install_fake_playwright(monkeypatch, fetch_behavior=setup)

    scraper = BrowserScraper(browser_type="firefox")
    result = await scraper.fetch("https://example.com")
    await scraper.close()

    assert result is not None
    assert result.html == ""
    assert result.error == "TimeoutError"


@pytest.mark.asyncio
async def test_fetch_returns_none_when_playwright_missing(monkeypatch):
    # Force the lazy import to fail.
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)

    scraper = BrowserScraper()
    result = await scraper.fetch("https://example.com")
    assert result is None
    assert scraper._unavailable is True


@pytest.mark.asyncio
async def test_viewport_locale_tz_and_headers_passed_to_context(monkeypatch):
    """new_context receives viewport + locale + tz; extra headers are set.

    For a Firefox launcher with a Chrome UA passed in, BrowserScraper
    coerces the UA back to its default Firefox UA (Camada 1 stealth
    reinforcement), so the emitted headers must NOT include ``sec-ch-ua``
    — a Chrome-only client hint. Sending it from a Firefox UA is exactly
    the kind of engine/UA mismatch CF bot-score penalizes.
    """

    def setup(page, response):
        page.goto = AsyncMock(return_value=response)
        page.content = AsyncMock(return_value="<html>ok</html>")

    _, browser = _install_fake_playwright(monkeypatch, fetch_behavior=setup)

    scraper = BrowserScraper(browser_type="firefox")
    chrome_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    result = await scraper.fetch("https://example.com", user_agent=chrome_ua)
    await scraper.close()

    assert result is not None
    assert result.html == "<html>ok</html>"

    call_kwargs = browser.new_context.call_args.kwargs
    assert call_kwargs.get("viewport") == {"width": 1920, "height": 1080}
    assert call_kwargs.get("locale") == "pt-BR"
    assert call_kwargs.get("timezone_id") == "America/Fortaleza"
    # The Chrome UA must have been swapped for the Firefox default —
    # otherwise navigator.userAgent disagrees with the TLS fingerprint.
    assert "Firefox/" in call_kwargs.get("user_agent", "")

    context_obj = browser.new_context.return_value
    context_obj.set_extra_http_headers.assert_awaited_once()
    headers_arg = context_obj.set_extra_http_headers.call_args.args[0]
    assert "Accept-Language" in headers_arg
    # Firefox does not emit client hints. Under a Firefox launcher we
    # must NOT send sec-ch-ua, even if the caller passed a Chrome UA.
    assert "sec-ch-ua" not in headers_arg


@pytest.mark.asyncio
async def test_chromium_launcher_keeps_chrome_ua_and_sec_ch_ua(monkeypatch):
    """Under a chromium launcher a Chrome UA is NOT coerced, and
    sec-ch-ua headers are populated from it."""

    def setup(page, response):
        page.goto = AsyncMock(return_value=response)
        page.content = AsyncMock(return_value="<html>ok</html>")

    _, browser = _install_fake_playwright(monkeypatch, fetch_behavior=setup)

    scraper = BrowserScraper(browser_type="chromium")
    chrome_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    await scraper.fetch("https://example.com", user_agent=chrome_ua)
    await scraper.close()

    call_kwargs = browser.new_context.call_args.kwargs
    assert call_kwargs.get("user_agent") == chrome_ua

    context_obj = browser.new_context.return_value
    headers_arg = context_obj.set_extra_http_headers.call_args.args[0]
    assert "sec-ch-ua" in headers_arg


def test_sec_ch_ua_chrome():
    from mapear_rss.extraction.browser_scraper import _sec_ch_ua_headers

    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    h = _sec_ch_ua_headers(ua)
    assert "sec-ch-ua" in h
    assert "131" in h["sec-ch-ua"]
    assert h["sec-ch-ua-mobile"] == "?0"
    assert h["sec-ch-ua-platform"] == '"Windows"'


def test_sec_ch_ua_firefox_empty():
    from mapear_rss.extraction.browser_scraper import _sec_ch_ua_headers

    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) "
        "Gecko/20100101 Firefox/132.0"
    )
    h = _sec_ch_ua_headers(ua)
    assert h == {}


def test_ua_rotation_diversity():
    """10 next() calls on default pool must return >= 3 distinct UAs."""
    from mapear_rss.extraction.user_agents import UserAgentRotator

    rot = UserAgentRotator(enabled=True)
    seen = {rot.next() for _ in range(10)}
    assert len(seen) >= 3
