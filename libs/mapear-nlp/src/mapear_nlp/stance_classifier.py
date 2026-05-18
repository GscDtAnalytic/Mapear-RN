"""Stance classifier over narrative summaries — Eixo 2 v2b.

Takes ``GoldArticle.narrative_summary`` (the 2-4 sentence LLM summary
from Eixo 2 v1) and classifies the stance toward the target official:

  favor   — narrative presents the official positively / approvingly
  contra  — narrative presents the official negatively / critically
  neutro  — narrative is factual / balanced / no clear position

Architecture mirrors the narrative explainer (Eixo 2 v1):

  narrative_summary ──► render prompt ──► LLMClient.complete
         │                                       │
         │                                       ▼
         ▼                                StanceResult (JSON-parsed)
  NarrativeCache (GCS, content-addressed, separate prefix)

Cache key: (content_hash, rule_version, stance_v1). Rotating the prompt
by bumping the PROMPT_VERSION constant invalidates the cache so old labels
remain queryable while new ones accumulate with the new key.

The classifier is best-effort: JSON parse errors and LLM errors return a
``StanceResult(stance_label=None, error=...)`` without raising.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from mapear_infra.privacy import RedactionLevel, redact

from mapear_nlp.llm.client import LLMClient, LLMError
from mapear_nlp.narrative_cache import NarrativeCache

PROMPT_VERSION = "stance_v1"
_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "eval" / "prompts" / "stance_v1.txt"
)
_VALID_STANCES = frozenset({"favor", "contra", "neutro"})
_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


@dataclass(frozen=True)
class StanceResult:
    """Outcome of one stance classification call.

    ``stance_label`` is None when the LLM call failed, returned
    unparseable JSON, or returned an unrecognised stance value.
    ``error`` carries the reason in that case.
    """

    stance_label: str | None  # "favor" | "contra" | "neutro" | None
    confidence: str | None  # "high" | "medium" | "low" | None
    prompt_version: str
    cache_hit: bool = False
    error: str | None = None
    redaction_level: str = "masked"
    redaction_counts: dict[str, int] = field(default_factory=dict)


def _load_prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _render_prompt(
    *,
    narrative_summary: str,
    person_name: str,
    person_role: str,
    template: str,
) -> str:
    return template.format(
        narrative_summary=narrative_summary or "(no narrative available)",
        person_name=person_name or "(unknown)",
        person_role=person_role or "(unknown)",
    )


def _parse_stance_json(raw: str) -> tuple[str | None, str | None, str | None]:
    """Parse LLM output into (stance_label, confidence, error).

    Expects: {"stance": "favor"|"contra"|"neutro", "confidence": "high"|"medium"|"low"}
    Returns error string on any parse failure.
    """
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        return None, None, f"JSON parse error: {exc} — raw={raw[:80]!r}"

    stance = parsed.get("stance")
    if stance not in _VALID_STANCES:
        return None, None, f"unrecognised stance={stance!r} — raw={raw[:80]!r}"

    confidence = parsed.get("confidence")
    if confidence not in _VALID_CONFIDENCE:
        confidence = None  # tolerate missing or unexpected confidence

    return stance, confidence, None


class StanceClassifier:
    """Coordinates cache lookup + prompt rendering + LLM call for stance.

    The concrete LLM client and cache are injected so tests can substitute
    in-memory stand-ins. The prompt template is loaded once per instance.
    Swap prompts by bumping ``PROMPT_VERSION`` and editing
    ``eval/prompts/stance_v1.txt`` (which changes the cache key).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        cache: NarrativeCache | None,
        *,
        max_tokens: int = 60,
        temperature: float = 0.1,
        timeout_seconds: float = 30.0,
        prompt_template: str | None = None,
        redaction_level: RedactionLevel = RedactionLevel.MASKED,
        hmac_key: bytes | None = None,
    ) -> None:
        self._llm = llm_client
        self._cache = cache
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout = timeout_seconds
        self._template = prompt_template or _load_prompt_template()
        self._redaction_level = redaction_level
        self._hmac_key = hmac_key

    def classify(
        self,
        *,
        content_hash: str,
        narrative_summary: str,
        person_name: str,
        person_role: str,
        rule_version: str,
    ) -> StanceResult:
        """Return a stance for one narrative. Caller must gate on narrative NOT NULL."""
        cache_key = NarrativeCache.make_key(
            content_hash=content_hash,
            rule_version=rule_version,
            prompt_version=PROMPT_VERSION,
        )

        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None and cached.get("stance_label") in _VALID_STANCES:
                return StanceResult(
                    stance_label=cached["stance_label"],
                    confidence=cached.get("confidence"),
                    prompt_version=PROMPT_VERSION,
                    cache_hit=True,
                    redaction_level=self._redaction_level.value,
                )

        # Eixo 6 light — redact PII before sending to external LLM.
        # person_name is intentionally exempt: public officials in office
        # are not PII under LGPD.
        narrative_red = redact(
            narrative_summary, level=self._redaction_level, hmac_key=self._hmac_key
        )
        counts = dict(narrative_red.counts)

        prompt = _render_prompt(
            narrative_summary=narrative_red.text,
            person_name=person_name,
            person_role=person_role,
            template=self._template,
        )

        try:
            raw = self._llm.complete(
                prompt,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                timeout_seconds=self._timeout,
            )
        except LLMError as exc:
            logger.warning(
                "Stance LLM call failed for {hash}: {err}",
                hash=content_hash[:12],
                err=exc,
            )
            return StanceResult(
                stance_label=None,
                confidence=None,
                prompt_version=PROMPT_VERSION,
                error=str(exc),
                redaction_level=self._redaction_level.value,
                redaction_counts=counts,
            )

        stance_label, confidence, parse_error = _parse_stance_json(raw)

        if parse_error:
            logger.warning(
                "Stance parse error for {hash}: {err}",
                hash=content_hash[:12],
                err=parse_error,
            )
            return StanceResult(
                stance_label=None,
                confidence=None,
                prompt_version=PROMPT_VERSION,
                error=parse_error,
                redaction_level=self._redaction_level.value,
                redaction_counts=counts,
            )

        if self._cache is not None:
            self._cache.set(
                cache_key,
                {
                    "stance_label": stance_label,
                    "confidence": confidence,
                    "prompt_version": PROMPT_VERSION,
                    "rule_version": rule_version,
                },
            )

        return StanceResult(
            stance_label=stance_label,
            confidence=confidence,
            prompt_version=PROMPT_VERSION,
            redaction_level=self._redaction_level.value,
            redaction_counts=counts,
        )
