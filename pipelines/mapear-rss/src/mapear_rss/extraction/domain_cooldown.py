"""Adaptive per-domain cooldown and retry policy.

Keeps an in-memory map of domain → cooldown_until timestamp. When a
domain returns a block signal, 403, or 429, it is parked for a growing
cooldown window so the scraper stops hammering it within the same run.

Cooldown decisions are gated by an error class:

- ``bot_block`` (HTTP 403, anti-bot signals): uses ``base_seconds`` with
  exponential growth capped at ``max_seconds`` (default 2h base, 4h cap).
- ``rate_limit`` (HTTP 429): uses ``rate_limit_base_seconds`` with
  growth capped at ``rate_limit_max_seconds`` (default 15m base, 1h cap).
- ``parser_hard`` (selector_missing, empty_content): deterministic 24h
  park on the first occurrence. The template is broken; backing off
  hard prevents wasting the rest of the run on a domain we can't
  extract from until a human fixes the selector.
- ``parser`` (parser_failure and other transient parser crashes):
  never parks the domain — retry budgets handle it.
- ``transient`` (timeout, 5xx, connection_reset): does not park either
  — retry budgets handle it.

A ``trigger_threshold`` protects against single false positives: the
domain is only parked after that many consecutive qualifying blocks.

Also centralizes the retry-count decision per error type so upstream
callers do not need to branch on error names.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal
from urllib.parse import urlparse

from loguru import logger

ErrorClass = Literal["bot_block", "rate_limit", "parser_hard", "parser", "transient"]


def error_class_for(error_type: str | None) -> ErrorClass:
    """Map a canonical error_type to a cooldown error class."""
    if error_type in ("blocked_bot", "http_403"):
        return "bot_block"
    if error_type == "http_429":
        return "rate_limit"
    if error_type in ("selector_missing", "empty_content"):
        return "parser_hard"
    if error_type == "parser_failure":
        return "parser"
    return "transient"


@dataclass
class _DomainState:
    cooldown_until: float = 0.0
    consecutive_blocks: int = 0
    total_skips: int = 0
    parser_flags: int = 0
    last_error_type: str | None = None
    last_error_class: ErrorClass | None = None


@dataclass
class DomainCooldown:
    """Per-domain cooldown with exponential growth on repeated blocks.

    ``trigger_threshold`` is the number of consecutive qualifying blocks
    required before the first cooldown window is armed. A value of 1
    reproduces the old (aggressive) behavior; the default of 1 is kept
    for backwards-compat in existing tests, but the scraper passes the
    value from ``SCRAPER_COOLDOWN_TRIGGER_THRESHOLD`` (default 2) so a
    single false positive no longer parks a domain.
    """

    base_seconds: float = 7200.0
    max_seconds: float = 14400.0
    growth_factor: float = 2.0
    rate_limit_base_seconds: float = 900.0
    rate_limit_max_seconds: float = 3600.0
    parser_hard_seconds: float = 21600.0
    trigger_threshold: int = 1
    parser_hard_trigger: int = 1
    parser_disabled: bool = True
    _state: dict[str, _DomainState] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)
    _applied_count: int = 0
    _reason_distribution: Counter = field(default_factory=Counter)

    @staticmethod
    def _domain(url: str) -> str:
        return urlparse(url).netloc

    def is_cool(self, url: str, now: float | None = None) -> bool:
        """Return True if the domain is NOT in cooldown (safe to fetch).

        When ``FORCE_SCRAPE=true`` is set in the environment, all domains are
        treated as cool regardless of their recorded state. This is an
        operator escape hatch for manual reprocessing runs.
        """
        if os.environ.get("FORCE_SCRAPE") == "true":
            return True
        now = now if now is not None else time.time()
        domain = self._domain(url)
        with self._lock:
            st = self._state.get(domain)
            if st is None:
                return True
            if st.cooldown_until <= now:
                return True
            st.total_skips += 1
            return False

    def record_block(
        self,
        url: str,
        error_type: str,
        *,
        error_class: ErrorClass | None = None,
        now: float | None = None,
    ) -> float:
        """Record a blocking event and maybe park the domain.

        Returns the ``cooldown_until`` timestamp if a window was armed,
        or ``0.0`` if the event did not trigger cooldown (pre-threshold,
        parser class, etc.). Parser-class events never park the domain
        when ``parser_disabled`` is True (the default).
        """
        now = now if now is not None else time.time()
        domain = self._domain(url)
        cls: ErrorClass = error_class or error_class_for(error_type)

        with self._lock:
            st = self._state.setdefault(domain, _DomainState())
            st.last_error_type = error_type
            st.last_error_class = cls

            if cls == "parser" and self.parser_disabled:
                st.parser_flags += 1
                return 0.0

            if cls == "transient":
                return 0.0

            if cls == "parser_hard":
                # Long park after parser_hard_trigger consecutive failures.
                # Requires ALL layers (httpx + browser) to have failed before
                # the caller records this block, so the threshold reflects true
                # parse failures across the full scraping stack.
                st.parser_flags += 1
                st.consecutive_blocks += 1
                if st.consecutive_blocks < self.parser_hard_trigger:
                    return 0.0
                st.cooldown_until = now + self.parser_hard_seconds
                self._applied_count += 1
                self._reason_distribution[cls] += 1
                return st.cooldown_until

            st.consecutive_blocks += 1
            if st.consecutive_blocks < self.trigger_threshold:
                return 0.0

            if cls == "rate_limit":
                base = self.rate_limit_base_seconds
                cap = self.rate_limit_max_seconds
            else:
                base = self.base_seconds
                cap = self.max_seconds
            growth_exp = st.consecutive_blocks - self.trigger_threshold
            window = min(cap, base * (self.growth_factor**growth_exp))
            st.cooldown_until = now + window
            self._applied_count += 1
            self._reason_distribution[cls] += 1
            return st.cooldown_until

    def record_success(self, url: str) -> None:
        domain = self._domain(url)
        with self._lock:
            st = self._state.get(domain)
            if st:
                st.consecutive_blocks = 0
                st.cooldown_until = 0.0
                st.last_error_type = None
                st.last_error_class = None

    def reset(self, force: bool = False) -> int:
        """Clear in-memory cooldown state. Returns count of domains released.

        ``force=True`` clears all state unconditionally (ignores whether any
        window was actually active). ``force=False`` only releases domains
        whose cooldown window is still in the future, leaving the
        ``consecutive_blocks`` counter intact so a brief recool needs fewer
        blocks to re-arm.
        """
        now = time.time()
        with self._lock:
            if force:
                released = len(self._state)
                self._state.clear()
                self._applied_count = 0
                self._reason_distribution.clear()
            else:
                released = 0
                for st in self._state.values():
                    if st.cooldown_until > now:
                        st.cooldown_until = 0.0
                        released += 1
        logger.info(
            "cooldown_reset urls_released={n} forced={forced}",
            n=released,
            forced=force,
        )
        return released

    def snapshot(self) -> dict[str, dict]:
        """Return a serializable view for the run report."""
        now = time.time()
        with self._lock:
            return {
                domain: {
                    "consecutive_blocks": st.consecutive_blocks,
                    "total_skips": st.total_skips,
                    "parser_flags": st.parser_flags,
                    "cooldown_remaining_s": max(0.0, st.cooldown_until - now),
                    "last_error_type": st.last_error_type,
                    "last_error_class": st.last_error_class,
                }
                for domain, st in self._state.items()
            }

    def total_skips(self) -> int:
        with self._lock:
            return sum(s.total_skips for s in self._state.values())

    def applied_count(self) -> int:
        with self._lock:
            return self._applied_count

    def reason_distribution(self) -> dict[str, int]:
        with self._lock:
            return dict(self._reason_distribution)


# Canonical retry-budget lookup. Values here are the MAXIMUM number of
# attempts (including the first) for a given error class. The scraper
# decides which class an error falls into using block_detector.classify_failure.
RETRY_BUDGET_DEFAULTS: dict[str, int] = {
    "timeout": 3,
    "connection_reset": 3,
    "http_5xx": 3,
    "http_429": 2,
    "http_403": 1,
    "blocked_bot": 1,
    "http_404": 1,
    "empty_content": 1,
    "selector_missing": 1,
    "unknown": 2,
}


def retry_budget(
    error_type: str,
    overrides: dict[str, int] | None = None,
) -> int:
    """Return the max attempts allowed for this error class."""
    if overrides and error_type in overrides:
        return overrides[error_type]
    return RETRY_BUDGET_DEFAULTS.get(error_type, 2)
