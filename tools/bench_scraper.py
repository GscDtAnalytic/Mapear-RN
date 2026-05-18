"""Benchmark a scraper strategy against a URL list.

This is the measurement harness that drives the layered anti-bot plan:
each camada adds a strategy here, and we compare success rates across
them on the same URL fixture.

Strategies::

    layer1              Reinforced Playwright BrowserScraper (Firefox + pt-BR
                        locale + America/Fortaleza TZ + warm-up + storage_state)
    layer2_camoufox     Camoufox (patched Firefox JS fingerprint; drop-in
                        Playwright API; defeats CF Bot Management bot signals)

Usage::

    # Bootstrap off the feed seeds (reuses the antibot_classifier
    # discovery helper so the URL set matches Camada 0's probe):
    python -m scripts.bench_scraper --strategy layer1 --from-feeds --per-feed 3
    python -m scripts.bench_scraper --strategy layer2_camoufox --from-feeds --per-feed 3

    # Real blocked-URL log, one URL per line:
    python -m scripts.bench_scraper --strategy layer2_camoufox --from-file blocked.txt

Output JSON shape (at ``--out``)::

    {
      "strategy": "layer1",
      "ran_at": "...",
      "total_urls": N,
      "success_rate": 0.62,
      "by_domain": {
        "tribunadonorte.com.br": {
          "attempted": 3,
          "succeeded": 2,
          "blocked": 1,
          "avg_latency_ms": 3812,
          "urls": [{"url": "...", "success": true, "status": 200, ...}]
        },
        ...
      }
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Wire up imports so the script works when run from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "Mapear-RSS" / "src"))
sys.path.insert(0, str(REPO_ROOT / "mapear-core" / "src"))

from diagnostics.antibot_classifier import (  # noqa: E402
    discover_urls_from_feeds,
    load_seed_feeds,
    read_urls_from_file,
    read_urls_from_stdin,
)


@dataclass
class UrlResult:
    url: str
    domain: str
    success: bool
    status: int | None
    latency_ms: int
    blocked: bool
    challenge: bool
    extractor_used: str | None
    error: str | None
    block_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "domain": self.domain,
            "success": self.success,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "blocked": self.blocked,
            "challenge": self.challenge,
            "extractor_used": self.extractor_used,
            "error": self.error,
            "block_evidence": self.block_evidence,
        }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


class Layer1Strategy:
    """Camada 1: reinforced Playwright ``BrowserScraper``.

    Uses the production module so the benchmark measures the same code
    that Cloud Run runs. No override of config — whatever
    ``SCRAPER_PLAYWRIGHT_*`` env vars are set (``HEADED``, warm-up
    tunables) are picked up by the shared ``BrowserScraper`` ctor
    defaults.
    """

    name = "layer1"

    def __init__(self) -> None:
        # Lazy imports so running with a strategy we don't use (once
        # Camadas 2+ land) doesn't blow up on missing deps.
        from mapear_rss.extraction.block_detector import detect
        from mapear_rss.extraction.browser_scraper import BrowserScraper

        self._detect = detect
        self._scraper = BrowserScraper()

    async def close(self) -> None:
        await self._scraper.close()

    async def fetch(self, url: str) -> UrlResult:
        domain = urlparse(url).netloc
        started = time.perf_counter()
        result = await self._scraper.fetch(url)
        latency_ms = int((time.perf_counter() - started) * 1000)

        if result is None:
            return UrlResult(
                url=url,
                domain=domain,
                success=False,
                status=None,
                latency_ms=latency_ms,
                blocked=False,
                challenge=False,
                extractor_used=None,
                error="playwright_unavailable",
            )
        if result.error:
            return UrlResult(
                url=url,
                domain=domain,
                success=False,
                status=result.status_code or None,
                latency_ms=latency_ms,
                blocked=False,
                challenge=False,
                extractor_used=None,
                error=result.error,
            )

        signals = self._detect(result.status_code, result.headers, result.html)
        blocked = signals.blocked
        # "success" here is deliberately loose: the response arrived, a
        # block detector did not flag a challenge, and HTTP status is
        # 2xx/3xx. Parser success is a separate concern — the benchmark
        # measures how often we get through the anti-bot wall, not how
        # often trafilatura finds an article body.
        status = result.status_code
        http_ok = status is not None and status < 400
        success = http_ok and not blocked

        return UrlResult(
            url=url,
            domain=domain,
            success=success,
            status=status,
            latency_ms=latency_ms,
            blocked=blocked,
            challenge=signals.js_challenge_detected or signals.captcha_detected,
            extractor_used="playwright_firefox_layer1",
            error=None,
            block_evidence=signals.markers_hit,
        )


class Layer2CamoufoxStrategy:
    """Camada 2: Camoufox — patched Firefox JS fingerprint.

    Camoufox removes every indicator of browser automation that Cloudflare
    Bot Management keys on (navigator.webdriver, CDP presence, font/canvas
    metrics, etc.).  The API is Playwright-compatible, so the block-detection
    logic is identical to Layer1.
    """

    name = "layer2_camoufox"

    def __init__(self) -> None:
        from mapear_rss.extraction.block_detector import detect

        self._detect = detect
        self._cm: Any = None
        self._browser: Any = None

    async def _get_browser(self) -> Any:
        if self._browser is None:
            from camoufox.async_api import AsyncCamoufox

            self._cm = AsyncCamoufox(headless=True, os="linux")
            self._browser = await self._cm.__aenter__()
        return self._browser

    async def close(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
            self._browser = None
            self._cm = None

    async def fetch(self, url: str) -> UrlResult:
        domain = urlparse(url).netloc
        started = time.perf_counter()
        try:
            browser = await self._get_browser()
            ctx = await browser.new_context(
                locale="pt-BR",
                timezone_id="America/Fortaleza",
                viewport={"width": 1920, "height": 1080},
            )
            page = await ctx.new_page()
            try:
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=20_000
                )
                html = await page.content()
            finally:
                await ctx.close()

            status = resp.status if resp else None
            headers = dict(resp.headers) if resp else {}
            latency_ms = int((time.perf_counter() - started) * 1000)
            signals = self._detect(status, headers, html)
            blocked = signals.blocked
            http_ok = status is not None and status < 400
            success = http_ok and not blocked
            return UrlResult(
                url=url,
                domain=domain,
                success=success,
                status=status,
                latency_ms=latency_ms,
                blocked=blocked,
                challenge=signals.js_challenge_detected or signals.captcha_detected,
                extractor_used="camoufox_layer2",
                error=None,
                block_evidence=signals.markers_hit,
            )
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.perf_counter() - started) * 1000)
            return UrlResult(
                url=url,
                domain=domain,
                success=False,
                status=None,
                latency_ms=latency_ms,
                blocked=False,
                challenge=False,
                extractor_used=None,
                error=f"{type(exc).__name__}: {str(exc)[:120]}",
            )


class Layer2bHttpxCFStrategy:
    """Camada 2b: plain httpx against CF-protected domains.

    Cloudflare managed challenge / Turnstile requires JS execution. A plain
    HTTP client cannot run JS, so CF either serves the real article HTML or a
    static challenge page that it cannot enforce without JS.  We measure which
    one happens in practice.

    Headers mimic Firefox desktop (no sec-ch-ua — Firefox doesn't send client
    hints; no brotli — system package not installed).

    Block classification note: the production block_detector flags ``cf-ray``
    headers as cloudflare_detected, but CF sends that header on ALL responses
    (not just challenge pages).  This strategy uses its own lighter classifier:
    a response is blocked only when the body contains hard challenge markers or
    the body is suspiciously small (<4 KB at HTTP 200).
    """

    name = "layer2b_httpx_cf"

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0"
        ),
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

    # Markers that only appear in real challenge/block pages, never in normal
    # article HTML.  Deliberately excludes bare "cloudflare" and "cf-ray"
    # (both appear in CDN-served pages as script paths and response headers).
    _HARD_BLOCK_MARKERS = (
        "checking your browser",
        "just a moment",
        "__cf_chl",
        "cf-chl-bypass",
        "jschl-answer",
        "challenge-form",
        "/cdn-cgi/challenge-platform",
        "g-recaptcha",
        "h-captcha",
        "hcaptcha",
        "please verify you are a human",
        "i'm not a robot",
        "access denied",
        "request blocked",
        "attention required",
    )

    _MIN_REAL_BODY_BYTES = 4096

    def __init__(self) -> None:
        import httpx

        self._httpx = httpx
        self._client: Any = None

    async def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._httpx.AsyncClient(
                headers=self._HEADERS,
                follow_redirects=True,
                timeout=20.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _classify(self, status: int, html: str) -> tuple[bool, bool, list[str]]:
        """Return (blocked, challenge, evidence_markers)."""
        if status >= 400:
            return True, False, [f"status:{status}"]

        body_lower = html[:8000].lower()
        evidence: list[str] = []
        challenge = False

        for marker in self._HARD_BLOCK_MARKERS:
            if marker in body_lower:
                evidence.append(f"body:{marker}")
                if "recaptcha" in marker or "hcaptcha" in marker or "robot" in marker:
                    challenge = True

        body_bytes = len(html.encode("utf-8", errors="ignore"))
        if body_bytes < self._MIN_REAL_BODY_BYTES and not evidence:
            evidence.append(f"body:too_small({body_bytes}B)")

        blocked = bool(evidence)
        return blocked, challenge, evidence

    async def fetch(self, url: str) -> UrlResult:
        domain = urlparse(url).netloc
        started = time.perf_counter()
        try:
            client = await self._get_client()
            resp = await client.get(url)
            html = resp.text
            status = resp.status_code
            latency_ms = int((time.perf_counter() - started) * 1000)

            blocked, challenge, evidence = self._classify(status, html)
            success = not blocked
            return UrlResult(
                url=url,
                domain=domain,
                success=success,
                status=status,
                latency_ms=latency_ms,
                blocked=blocked,
                challenge=challenge,
                extractor_used="httpx_layer2b",
                error=None,
                block_evidence=evidence,
            )
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.perf_counter() - started) * 1000)
            return UrlResult(
                url=url,
                domain=domain,
                success=False,
                status=None,
                latency_ms=latency_ms,
                blocked=False,
                challenge=False,
                extractor_used=None,
                error=f"{type(exc).__name__}: {str(exc)[:120]}",
            )


STRATEGIES = {
    "layer1": Layer1Strategy,
    "layer2_camoufox": Layer2CamoufoxStrategy,
    "layer2b_httpx_cf": Layer2bHttpxCFStrategy,
}


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


async def run_bench(
    strategy_name: str,
    urls: list[str],
    concurrency: int,
) -> dict:
    strategy_cls = STRATEGIES[strategy_name]
    strategy = strategy_cls()

    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(url: str) -> UrlResult:
        async with sem:
            try:
                return await strategy.fetch(url)
            except Exception as exc:  # noqa: BLE001 — bench must not raise
                return UrlResult(
                    url=url,
                    domain=urlparse(url).netloc,
                    success=False,
                    status=None,
                    latency_ms=0,
                    blocked=False,
                    challenge=False,
                    extractor_used=None,
                    error=f"{type(exc).__name__}: {exc}",
                )

    try:
        started = time.perf_counter()
        results = await asyncio.gather(*(one(u) for u in urls))
        duration_s = round(time.perf_counter() - started, 2)
    finally:
        await strategy.close()

    return build_report(strategy_name, urls, results, duration_s)


def build_report(
    strategy: str,
    urls: list[str],
    results: list[UrlResult],
    duration_s: float,
) -> dict:
    by_domain: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "attempted": 0,
            "succeeded": 0,
            "blocked": 0,
            "challenge": 0,
            "errors": 0,
            "latencies": [],
            "urls": [],
        }
    )
    for r in results:
        d = by_domain[r.domain]
        d["attempted"] += 1
        if r.success:
            d["succeeded"] += 1
        if r.blocked:
            d["blocked"] += 1
        if r.challenge:
            d["challenge"] += 1
        if r.error:
            d["errors"] += 1
        if r.latency_ms:
            d["latencies"].append(r.latency_ms)
        d["urls"].append(r.to_dict())

    # Finalize per-domain stats.
    for domain, d in by_domain.items():
        lat = d.pop("latencies")
        d["avg_latency_ms"] = int(sum(lat) / len(lat)) if lat else 0
        d["success_rate"] = round(d["succeeded"] / d["attempted"], 3)

    total = len(results)
    succeeded = sum(1 for r in results if r.success)
    blocked = sum(1 for r in results if r.blocked)
    errors = sum(1 for r in results if r.error)

    return {
        "strategy": strategy,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": duration_s,
        "total_urls": total,
        "total_domains": len(by_domain),
        "succeeded": succeeded,
        "blocked": blocked,
        "errors": errors,
        "success_rate": round(succeeded / total, 3) if total else 0.0,
        "block_rate": round(blocked / total, 3) if total else 0.0,
        "by_domain": dict(by_domain),
        "urls_input_sample": urls[:5],
    }


def print_summary(report: dict) -> None:
    print(
        json.dumps(
            {
                "strategy": report["strategy"],
                "total_urls": report["total_urls"],
                "success_rate": report["success_rate"],
                "block_rate": report["block_rate"],
                "errors": report["errors"],
                "duration_s": report["duration_s"],
                "by_domain_rates": {
                    d: f"{v['succeeded']}/{v['attempted']}"
                    for d, v in sorted(
                        report["by_domain"].items(),
                        key=lambda kv: kv[1]["success_rate"],
                    )
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--strategy",
        choices=list(STRATEGIES.keys()),
        default="layer1",
        help="Which scraper strategy to benchmark.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--from-feeds",
        action="store_true",
        help="Discover URLs from Mapear-RSS seed feeds.",
    )
    src.add_argument(
        "--from-file",
        type=Path,
        help="One URL per line.",
    )
    src.add_argument(
        "--from-stdin",
        action="store_true",
    )
    p.add_argument("--per-feed", type=int, default=3)
    p.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help=(
            "Parallel fetches. Defaults to 2, matching the production "
            "BrowserScraper semaphore. Higher values evict the browser's "
            "page pool and inflate variance."
        ),
    )
    p.add_argument(
        "--max-urls",
        type=int,
        default=0,
        help="Cap the URL list (0 = no cap). Useful for quick smoke runs.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON path. Defaults to diagnostics/bench_<strategy>.json.",
    )
    return p.parse_args(argv)


async def gather_urls(args: argparse.Namespace) -> list[str]:
    if args.from_feeds:
        feeds = load_seed_feeds()
        urls = await discover_urls_from_feeds(feeds, per_feed=args.per_feed)
    elif args.from_file:
        urls = read_urls_from_file(args.from_file)
    else:
        urls = read_urls_from_stdin()

    if args.max_urls and len(urls) > args.max_urls:
        urls = urls[: args.max_urls]
    return urls


async def main_async(args: argparse.Namespace) -> dict:
    if args.out is None:
        args.out = Path(f"diagnostics/bench_{args.strategy}.json")
    urls = await gather_urls(args)
    if not urls:
        raise SystemExit("No URLs to benchmark.")
    print(
        f"[bench] strategy={args.strategy} urls={len(urls)} "
        f"concurrency={args.concurrency}",
        file=sys.stderr,
    )
    report = await run_bench(args.strategy, urls, args.concurrency)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"[bench] wrote {args.out}", file=sys.stderr)
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(main_async(args))
    print_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
