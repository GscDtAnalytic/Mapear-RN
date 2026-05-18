"""PII redaction for content leaving the warehouse (Eixo 6 light).

Defensive layer between the pipeline and any external sink that
receives raw article content (Eixo 2 v1 sends content to Anthropic's
Messages API). Redacts Brazilian PII patterns — email, telefone (BR
mobile + landline), CPF, CNPJ — under one of four levels:

  NONE          — passthrough; tests only.
  MASKED        — replace with a stable token like "[email]" / "[cpf]".
  PSEUDONYMIZED — replace with "[email:HMAC8]"; same input always yields
                  same tag (per HMAC key), so analysts can correlate
                  redacted hits across documents without recovering the
                  original value.
  DROPPED       — remove the match entirely.

Public figures named in their public capacity (Fátima Bezerra, the
governor; João Maia, the deputado) are NOT PII for LGPD purposes —
they're public officials acting in office. We deliberately do NOT
redact person names. The redactor targets values that *could* be
private data: contact methods + identification numbers.

LGPD context: dados pessoais sensíveis (CPF) carry stricter treatment
under Art. 11; emails / phones are dados pessoais under Art. 5(I).
Sending them to a US-based LLM provider without redaction would
require a documented legitimate-interest basis + an international
transfer agreement (Art. 33). Easier to redact.

Limitations (deferred to Eixo 6 full):
  - RG (variable per state, no national format).
  - Endereço literal (rua / número / CEP) — needs entity detection.
  - DOB / family member names — needs ML-based PII detection.
  - Brazilian voter registration title (título de eleitor).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import Enum
from re import Match


class RedactionLevel(str, Enum):
    NONE = "none"
    MASKED = "masked"
    PSEUDONYMIZED = "pseudonymized"
    DROPPED = "dropped"


# --- BR-specific patterns ---------------------------------------------------
# CPF / CNPJ allow optional separators because both formats are common in
# news articles. The boundary anchors (?<!\d) / (?!\d) avoid swallowing
# digits from longer ID sequences (e.g. process numbers).

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Email — applied first, narrowest format.
    (
        "email",
        re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    ),
    # Formatted CPF / CNPJ — the separators disambiguate them from
    # phone numbers and from each other.
    (
        "cnpj",
        re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)"),
    ),
    (
        "cpf",
        re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)"),
    ),
    # Phone — matched before bare CPF/CNPJ because an 11-digit BR
    # mobile is structurally indistinguishable from a bare CPF. The
    # phone pattern requires either a +55 prefix, parenthesised area
    # code, or the leading "9" of a mobile to fire, limiting false
    # positives on accidental long digit runs.
    (
        "phone",
        re.compile(r"(?<!\d)(?:\+55\s*)?\(?\d{2}\)?\s*9\d{4}[-.\s]?\d{4}(?!\d)"),
    ),
    (
        "phone",
        re.compile(r"(?<!\d)(?:\+55\s*)?\(?\d{2}\)?\s*\d{4}[-.\s]?\d{4}(?!\d)"),
    ),
    # Bare CNPJ / CPF — last-resort catch-all for unformatted IDs. The
    # earlier phone patterns will have already eaten anything that
    # could plausibly be a phone number.
    (
        "cnpj",
        re.compile(r"(?<!\d)\d{14}(?!\d)"),
    ),
    (
        "cpf",
        re.compile(r"(?<!\d)\d{11}(?!\d)"),
    ),
]

# Public utility values that match the CPF/CNPJ digit-only patterns but
# carry no PII risk. Keep an explicit allowlist to avoid mangling things
# like "00000000000" placeholders in test data.
_ALLOWLIST = {
    "00000000000",
    "11111111111",
    "12345678901",
    "00000000000000",
    "12345678000100",
}


@dataclass(frozen=True)
class RedactionResult:
    """Output of a redact() call.

    ``text`` is the redacted string. ``counts`` maps category → number of
    matches replaced. Useful for the audit log so an analyst can see at
    a glance whether anything was scrubbed.
    """

    text: str
    counts: dict[str, int]

    @property
    def total_redactions(self) -> int:
        return sum(self.counts.values())


def _token(category: str, original: str, level: RedactionLevel, hmac_key: bytes) -> str:
    if level is RedactionLevel.DROPPED:
        return ""
    if level is RedactionLevel.MASKED:
        return f"[{category}]"
    if level is RedactionLevel.PSEUDONYMIZED:
        digest = hmac.new(hmac_key, original.encode("utf-8"), hashlib.sha256)
        return f"[{category}:{digest.hexdigest()[:8]}]"
    # NONE — caller should have short-circuited; keep input as-is.
    return original


def redact(
    text: str,
    *,
    level: RedactionLevel = RedactionLevel.MASKED,
    hmac_key: bytes | None = None,
) -> RedactionResult:
    """Apply PII redaction at the requested level.

    ``hmac_key`` is required only for PSEUDONYMIZED level — the same
    key must be used across runs to keep tags stable. Passing an empty
    key with PSEUDONYMIZED level raises ValueError to fail loud rather
    than silently leak with a default key.
    """
    if level is RedactionLevel.NONE or not text:
        return RedactionResult(text=text, counts={})
    if level is RedactionLevel.PSEUDONYMIZED and not hmac_key:
        raise ValueError(
            "PSEUDONYMIZED level requires hmac_key (set MAPEAR_LLM_PII_HMAC_KEY)"
        )

    counts: dict[str, int] = {}
    key = hmac_key or b""

    def make_sub(category: str):
        def _sub(match: Match[str]) -> str:
            value = match.group(0)
            if value in _ALLOWLIST:
                return value
            counts[category] = counts.get(category, 0) + 1
            return _token(category, value, level, key)

        return _sub

    redacted = text
    for category, pattern in _PATTERNS:
        redacted = pattern.sub(make_sub(category), redacted)
    return RedactionResult(text=redacted, counts=counts)


def parse_level(raw: str | None) -> RedactionLevel:
    """Coerce a settings string to RedactionLevel; defaults to MASKED."""
    if raw is None or raw == "":
        return RedactionLevel.MASKED
    try:
        return RedactionLevel(raw.lower())
    except ValueError as exc:
        raise ValueError(
            f"Unknown MAPEAR_LLM_PII_LEVEL={raw!r}. "
            f"Valid: {[lvl.value for lvl in RedactionLevel]}"
        ) from exc
