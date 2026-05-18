"""Per-URL diagnostic records and run-level aggregation.

Each fetch attempt produces a DiagnosticRecord with a bounded set of
fields (no raw HTML, hashed body prefix only). DiagnosticCollector
accumulates records and renders both a structured per-run report and
log-ready summaries.

The goal is evidence — once this is in production we should be able
to answer 'is example.com blocked or is the parser broken?' from the
logs alone.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from loguru import logger

# Header allowlist: only these header names are ever logged/stored, so we
# never leak set-cookie, auth, or other sensitive values.
HEADER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "server",
        "cf-ray",
        "cf-cache-status",
        "x-cache",
        "content-type",
        "content-length",
        "content-encoding",
        "via",
        "age",
        "x-powered-by",
    }
)

BODY_SAMPLE_BYTES = 4096

RUN_REPORT_VERSION = "2"
RUN_REPORT_AGGREGATION_METHOD = "explicit_counters_v2"


@dataclass
class FetchCounters:
    """Explicit counters for the run report.

    Split by source (main pending queue vs. retry queue) so the
    ``extraction_success_rate`` invariant ``extracted ≤ fetched`` can
    actually be enforced — the previous code mixed ``len(pending)`` with
    ``len(articles)`` (which included retry articles) and could produce
    rates > 1.
    """

    fetched_main: int = 0
    fetched_retry: int = 0
    fetched_unique_urls: int = 0
    extracted_main: int = 0
    extracted_retry: int = 0
    # Headless-render fallback counters (B7). ``browser_attempts`` is the
    # number of URLs handed to the Playwright path after httpx gave up,
    # ``browser_success`` the subset where render + parse succeeded, and
    # ``browser_failed`` the rest. All three are always zero when
    # ``SCRAPER_PLAYWRIGHT_ENABLED=false``.
    browser_attempts: int = 0
    browser_success: int = 0
    browser_failed: int = 0

    @property
    def fetched_total(self) -> int:
        return self.fetched_main + self.fetched_retry

    @property
    def extracted_total(self) -> int:
        return self.extracted_main + self.extracted_retry


def hash_body_sample(body: str | None) -> str | None:
    """Return a sha256 hex digest of a bounded body prefix, or None."""
    if not body:
        return None
    prefix = body[:BODY_SAMPLE_BYTES].encode("utf-8", errors="ignore")
    return hashlib.sha256(prefix).hexdigest()


def filter_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {
        k.lower(): str(v) for k, v in headers.items() if k.lower() in HEADER_ALLOWLIST
    }


@dataclass
class DiagnosticRecord:
    """One row per fetch attempt. Keep this small and JSON-serializable."""

    url: str
    domain: str
    attempt: int
    stage: str  # "fetch" | "parse" | "retry" | "cooldown_skip"
    status_code: int | None = None
    final_url: str | None = None
    latency_ms: int | None = None
    content_length_bytes: int | None = None
    body_sample_hash: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    block_signals: dict[str, Any] = field(default_factory=dict)
    extractor_used: str | None = None
    extracted_chars: int = 0
    extraction_success: bool = False
    error_type: str | None = None
    error_detail: str | None = None
    user_agent: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class DiagnosticCollector:
    """Accumulates per-URL records and builds the final run report.

    Thread-safe only under the GIL for append — no cross-process
    sharing. The scraper's async path runs in a single event loop so
    this is fine in practice.
    """

    def __init__(
        self,
        sample_rate: float = 1.0,
        debug_domains: frozenset[str] | None = None,
    ):
        self.sample_rate = sample_rate
        self.debug_domains = debug_domains or frozenset()
        self.records: list[DiagnosticRecord] = []
        self.fallback_save_count: int = 0
        self.retries_total: int = 0
        # Per-domain parser-recovery counter: {domain: {strategy: count}}
        self.parser_recovery: dict[str, Counter] = defaultdict(Counter)

    @staticmethod
    def domain_of(url: str) -> str:
        return urlparse(url).netloc

    def should_emit_debug(self, url: str) -> bool:
        return self.domain_of(url) in self.debug_domains

    def record(self, rec: DiagnosticRecord) -> None:
        self.records.append(rec)
        # Debug domains always get a structured log line.
        if rec.domain in self.debug_domains:
            logger.debug(
                "diag {payload}", payload=json.dumps(rec.to_dict(), default=str)
            )

    def note_fallback_save(self) -> None:
        self.fallback_save_count += 1

    def note_retry(self) -> None:
        self.retries_total += 1

    def note_parser_recovery(self, domain: str, strategy: str) -> None:
        """Record that ``strategy`` recovered content for ``domain``.

        Strategies are labels like ``trafilatura_recall``, ``jsonld``,
        ``readability``. Used for the per-domain parser_recovery_count
        breakdown in the run report so we can tell which fallback is
        actually earning its keep on problem domains (e.g. agorarn).
        """
        self.parser_recovery[domain][strategy] += 1

    # -------------------------------------------------------------- report --

    def _per_domain(self) -> dict[str, dict[str, Any]]:
        by_domain: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "attempts": 0,
                "success": 0,
                "blocked": 0,
                "parser_failure": 0,
                "http_error": 0,
                "error_types": Counter(),
            }
        )
        for r in self.records:
            d = by_domain[r.domain]
            d["attempts"] += 1
            if r.extraction_success:
                d["success"] += 1
            if r.block_signals.get("blocked"):
                d["blocked"] += 1
            if r.error_type and r.error_type.startswith("http_"):
                d["http_error"] += 1
            if r.error_type in ("selector_missing", "empty_content"):
                d["parser_failure"] += 1
            if r.error_type:
                d["error_types"][r.error_type] += 1

        # Finalize: compute rates and cast Counter → dict for JSON.
        for domain, d in by_domain.items():
            attempts = d["attempts"] or 1
            d["blocked_rate"] = round(d["blocked"] / attempts, 3)
            d["parser_failure_rate"] = round(d["parser_failure"] / attempts, 3)
            d["http_error_rate"] = round(d["http_error"] / attempts, 3)
            d["error_types"] = dict(d["error_types"])
            recovery = self.parser_recovery.get(domain)
            d["parser_recovery_count"] = dict(recovery) if recovery else {}
        return dict(by_domain)

    def build_report(
        self,
        *,
        discovered: int,
        unique: int,
        rn_relevant: int,
        cooldown_skips: int,
        counters: FetchCounters | None = None,
        # Legacy aggregated kwargs are still accepted so existing callers
        # and tests work without modification. When ``counters`` is None
        # we build one from ``fetched`` / ``extracted`` and mark retry
        # counts as zero.
        fetched: int | None = None,
        extracted: int | None = None,
        cooldown_applied_count: int = 0,
        cooldown_reason_distribution: dict[str, int] | None = None,
        deferred_by_cooldown: int = 0,
        diversity: dict[str, Any] | None = None,
        feed_health: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if counters is None:
            counters = FetchCounters(
                fetched_main=int(fetched or 0),
                fetched_retry=0,
                fetched_unique_urls=int(fetched or 0),
                extracted_main=int(extracted or 0),
                extracted_retry=0,
            )

        per_domain = self._per_domain()

        # Top N domains with the most failures + their dominant cause.
        failed_domains = sorted(
            (
                (
                    domain,
                    d["attempts"] - d["success"],
                    (
                        max(d["error_types"].items(), key=lambda kv: kv[1])[0]
                        if d["error_types"]
                        else "none"
                    ),
                )
                for domain, d in per_domain.items()
                if d["attempts"] - d["success"] > 0
            ),
            key=lambda t: t[1],
            reverse=True,
        )[:10]

        blocked_domains = {
            domain: {
                "blocked_attempts": d["blocked"],
                "primary_error": (
                    max(d["error_types"].items(), key=lambda kv: kv[1])[0]
                    if d["error_types"]
                    else None
                ),
            }
            for domain, d in per_domain.items()
            if d["blocked"] > 0
        }

        fetched_total = counters.fetched_total
        extracted_total = counters.extracted_total

        integrity_warning = False
        if fetched_total > 0 and extracted_total > fetched_total:
            integrity_warning = True
            logger.error(
                "run_report invariant broken: extracted={extracted} > "
                "fetched={fetched}. Clamping extraction_success_rate to 1.0.",
                extracted=extracted_total,
                fetched=fetched_total,
            )

        if fetched_total > 0:
            raw_rate = extracted_total / fetched_total
            extraction_rate = round(max(0.0, min(1.0, raw_rate)), 3)
        else:
            extraction_rate = 0.0

        return {
            "report_version": RUN_REPORT_VERSION,
            "aggregation_method": RUN_REPORT_AGGREGATION_METHOD,
            "discovered": discovered,
            # Aggregated fields kept for BigQuery / dashboards backwards
            # compatibility. Always clamped.
            "fetched": fetched_total,
            "extracted": min(extracted_total, fetched_total) if fetched_total else 0,
            # Explicit v2 counters — the source of truth.
            "fetched_main": counters.fetched_main,
            "fetched_retry": counters.fetched_retry,
            "fetched_unique_urls": counters.fetched_unique_urls,
            "extracted_main": counters.extracted_main,
            "extracted_retry": counters.extracted_retry,
            "browser_attempts": counters.browser_attempts,
            "browser_success": counters.browser_success,
            "browser_failed": counters.browser_failed,
            "unique": unique,
            "rn_relevant": rn_relevant,
            "extraction_success_rate": extraction_rate,
            "integrity_warning": integrity_warning,
            "top_failed_domains": [
                {"domain": d, "failures": f, "primary_cause": c}
                for d, f, c in failed_domains
            ],
            "blocked_domains": blocked_domains,
            "fallback_save_count": self.fallback_save_count,
            "retries_total": self.retries_total,
            "cooldown_skips": cooldown_skips,
            "cooldown_applied_count": cooldown_applied_count,
            "cooldown_reason_distribution": dict(cooldown_reason_distribution or {}),
            "deferred_by_cooldown": deferred_by_cooldown,
            "per_domain": per_domain,
            "diversity": diversity or {},
            "feed_health": feed_health or {},
        }

    def log_report(self, report: dict[str, Any]) -> None:
        """Emit the full report as a single structured log line."""
        logger.info("run_report {payload}", payload=json.dumps(report, default=str))
