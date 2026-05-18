"""Post-fetch classifier that distinguishes anti-bot blocks from parser failures.

Looks at HTTP status, response headers and a small HTML prefix to flag
Cloudflare challenges, captcha walls, generic WAF pages and empty/truncated
bodies. The goal is to separate 'the site gave us nothing usable' (bot
block) from 'the site gave us HTML but our parser could not extract it'
(parser failure), so upstream retry/cooldown logic and the final report
can act on the distinction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

# Markers that only appear in genuine CF challenge/block pages.
# Deliberately excludes "cloudflare" (present in footer scripts of any CF-CDN
# page) and "cf-ray" (a response header value, not body text) — both cause
# false positives on real article HTML served through the CF CDN.
CLOUDFLARE_MARKERS = (
    "cf-chl-bypass",
    "__cf_chl",
    "_cf_chl_opt",
    "checking your browser",
    "just a moment",
    "attention required",
)
CAPTCHA_MARKERS = (
    "g-recaptcha",
    "h-captcha",
    "recaptcha",
    "hcaptcha",
    "please verify you are a human",
    "i'm not a robot",
    "turnstile",
)
JS_CHALLENGE_MARKERS = (
    "jschl-answer",
    "challenge-form",
    "challenge-platform",
    "/cdn-cgi/challenge-platform",
)
WAF_MARKERS = (
    "access denied",
    "request blocked",
    "you don't have permission",
    "incapsula",
    "sucuri",
    "akamai reference",
    "datadome",
    "perimetrix",
)

MIN_BODY_BYTES = 512
TRUNCATED_BYTES = 2048


@dataclass
class BlockSignals:
    """Flags emitted by the block detector for a single response."""

    cloudflare_detected: bool = False
    captcha_detected: bool = False
    js_challenge_detected: bool = False
    waf_detected: bool = False
    empty_body: bool = False
    truncated_body: bool = False
    # Telemetry-only flag: set when the browser render fallback was
    # attempted for this URL. Does not influence ``blocked``.
    browser_required: bool = False
    markers_hit: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return (
            self.cloudflare_detected
            or self.captcha_detected
            or self.js_challenge_detected
            or self.waf_detected
        )

    def to_dict(self) -> dict:
        return {
            "cloudflare_detected": self.cloudflare_detected,
            "captcha_detected": self.captcha_detected,
            "js_challenge_detected": self.js_challenge_detected,
            "waf_detected": self.waf_detected,
            "empty_body": self.empty_body,
            "truncated_body": self.truncated_body,
            "browser_required": self.browser_required,
            "blocked": self.blocked,
            "markers_hit": sorted(set(self.markers_hit)),
        }


def detect(
    status_code: int | None,
    headers: Mapping[str, str] | None,
    body: str | None,
) -> BlockSignals:
    """Classify a response into anti-bot block signals.

    Accepts partial inputs so it is safe to call on errored fetches too.
    The caller is responsible for only passing a bounded body prefix —
    this function does not slice on its own.
    """
    signals = BlockSignals()

    body = body or ""
    body_len = len(body.encode("utf-8", errors="ignore"))
    lower = body.lower()

    header_lower: dict[str, str] = {}
    if headers:
        for k, v in headers.items():
            header_lower[k.lower()] = str(v).lower()

    # cf-ray appears on ALL CF responses (CDN presence, not a block signal).
    if "cf-ray" in header_lower:
        signals.markers_hit.append("header:cf-ray")

    server = header_lower.get("server", "")
    if "cloudflare" in server:
        signals.markers_hit.append("header:server=cloudflare")

    # cf-mitigated: challenge is a specific mitigation header, unlike cf-ray.
    # It only appears when CF actively intercepted the request with a challenge.
    cf_mitigated = header_lower.get("cf-mitigated", "")
    if "challenge" in cf_mitigated:
        signals.cloudflare_detected = True
        signals.markers_hit.append("header:cf-mitigated=challenge")

    for marker in CLOUDFLARE_MARKERS:
        if marker in lower:
            signals.cloudflare_detected = True
            signals.markers_hit.append(f"body:{marker}")
            break

    for marker in CAPTCHA_MARKERS:
        if marker in lower:
            signals.captcha_detected = True
            signals.markers_hit.append(f"body:{marker}")
            break

    for marker in JS_CHALLENGE_MARKERS:
        if marker in lower:
            signals.js_challenge_detected = True
            signals.markers_hit.append(f"body:{marker}")
            break

    for marker in WAF_MARKERS:
        if marker in lower:
            signals.waf_detected = True
            signals.markers_hit.append(f"body:{marker}")
            break

    if body_len == 0:
        signals.empty_body = True
    elif body_len < MIN_BODY_BYTES:
        signals.truncated_body = True

    # A 403/429/503 with a tiny HTML body is almost always a WAF block.
    if (
        status_code in (403, 429, 503)
        and body_len < TRUNCATED_BYTES
        and not signals.blocked
    ):
        signals.waf_detected = True
        signals.markers_hit.append(f"status:{status_code}+small_body")

    return signals


def classify_failure(
    status_code: int | None,
    signals: BlockSignals,
    extracted_chars: int,
    exception_name: str | None = None,
) -> str:
    """Return a canonical error_type label for telemetry.

    Priority: transport exception > block signal > http error > parser.
    """
    if exception_name:
        lower = exception_name.lower()
        if "timeout" in lower:
            return "timeout"
        if "connect" in lower or "reset" in lower:
            return "connection_reset"
        if "ssl" in lower:
            return "ssl_error"

    if signals.blocked:
        return "blocked_bot"

    if status_code is not None:
        if status_code == 403:
            return "http_403"
        if status_code == 429:
            return "http_429"
        if status_code == 404:
            return "http_404"
        if 500 <= status_code < 600:
            return "http_5xx"
        if 400 <= status_code < 500:
            return f"http_{status_code}"

    if signals.empty_body:
        return "empty_content"

    if extracted_chars == 0:
        return "selector_missing"

    return "unknown"
