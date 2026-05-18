"""Lightweight language detection and code normalization for social posts.

Uses langdetect (port of Google's language-detect) as the detection backend.
Detection is deterministic (DetectorFactory.seed = 0) for reproducible results.

Policy:
- Platform-provided code → normalize then trust it (reason="api" or "api_normalized").
- Null code + non-empty text → detect (reason="detected" or "detection_failed").
- Empty/null text → language=None, reason="empty_text".
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

try:
    from langdetect import (  # type: ignore[import]
        DetectorFactory,
        LangDetectException,
        detect_langs,  # type: ignore[import]
    )

    DetectorFactory.seed = 0
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False

# Codes that mean "language unknown or not applicable".
# Sources: X API v2 (qme, qam), BCP 47 (und), ISO 639-2 (zxx), RFC 5646 (mul, mis).
_UNKNOWN_CODES: frozenset[str] = frozenset(
    {"qme", "zxx", "und", "qam", "mul", "mis", "art"}
)


@dataclass(frozen=True)
class LanguageDetection:
    language: str | None  # ISO 639-1 base code ("pt", "en") or "und" or None
    confidence: float | None  # [0, 1] when detected; None for API-provided codes
    reason: str  # see values below

    # reason values:
    # "api"            — platform code kept as-is (already canonical)
    # "api_normalized" — platform code normalized (e.g. "qme" → "und", "pt-BR" → "pt")
    # "detected"       — client-side detection succeeded
    # "empty_text"     — text is blank; language cannot be inferred
    # "detection_failed" — langdetect raised LangDetectException (too short/ambiguous)
    # "no_detector"    — langdetect not installed


def _normalize_code(raw: str) -> str:
    """Strip region/script subtags and map unknown codes to 'und'.

    >>> _normalize_code("pt-BR")
    'pt'
    >>> _normalize_code("qme")
    'und'
    >>> _normalize_code("  PT  ")
    'pt'
    """
    base = raw.strip().lower().split("-")[0].split("_")[0]
    if not base or base in _UNKNOWN_CODES:
        return "und"
    return base


def detect_and_normalize(
    text: str,
    platform_language: str | None,
) -> LanguageDetection:
    """Determine the language of a social post.

    If the platform already provides a language code, normalize it.
    Otherwise run client-side detection on the post text.
    """
    if platform_language:
        normalized = _normalize_code(platform_language)
        raw_base = platform_language.strip().lower().split("-")[0].split("_")[0]
        reason = "api" if normalized == raw_base else "api_normalized"
        return LanguageDetection(language=normalized, confidence=None, reason=reason)

    stripped = text.strip()
    if not stripped:
        return LanguageDetection(language=None, confidence=None, reason="empty_text")

    if not _HAS_LANGDETECT:
        return LanguageDetection(language=None, confidence=None, reason="no_detector")

    try:
        results = detect_langs(stripped)  # type: ignore[possibly-undefined]
        if not results:
            return LanguageDetection(
                language=None, confidence=None, reason="detection_failed"
            )
        top = results[0]
        return LanguageDetection(
            language=_normalize_code(top.lang),
            confidence=round(top.prob, 4),
            reason="detected",
        )
    except LangDetectException:  # type: ignore[possibly-undefined]
        logger.debug(
            "langdetect: could not detect language for text len={}", len(stripped)
        )
        return LanguageDetection(
            language=None, confidence=None, reason="detection_failed"
        )
