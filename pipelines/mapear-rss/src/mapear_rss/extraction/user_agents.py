"""User-Agent rotation pool.

Small, curated list of stable, realistic desktop browser UAs. Kept in
code (versioned) rather than an external file so the rotation is
deterministic and auditable. Rotation is round-robin with optional
deterministic seeding for tests.
"""

from __future__ import annotations

import itertools
import random
from threading import Lock

# Keep this list short and realistic. Update deliberately — each entry is
# a claim we make about the client and sites fingerprint these.
USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Edge on Windows 11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.2903.112",
    # Chrome 132 on Windows 11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
)


class UserAgentRotator:
    """Thread-safe round-robin rotator over USER_AGENTS."""

    def __init__(
        self,
        enabled: bool = True,
        seed: int | None = None,
        pool: tuple[str, ...] = USER_AGENTS,
    ) -> None:
        self.enabled = enabled
        self._pool = list(pool)
        if seed is not None:
            rnd = random.Random(seed)
            rnd.shuffle(self._pool)
        self._cycle = itertools.cycle(self._pool)
        self._lock = Lock()
        self._default = self._pool[0]

    def next(self) -> str:
        if not self.enabled:
            return self._default
        with self._lock:
            return next(self._cycle)

    def for_domain(self, domain: str) -> str:
        """Pin a UA per domain so repeated requests look consistent.

        Uses a stable hash so the same domain always gets the same UA
        within a process, which reduces fingerprint churn mid-session.
        """
        if not self.enabled:
            return self._default
        idx = (hash(domain) & 0xFFFF) % len(self._pool)
        return self._pool[idx]
