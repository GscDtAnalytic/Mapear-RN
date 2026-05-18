"""LLM-as-explainer over the political sentiment classifier — Eixo 2 v1.

The classifier (``political_sentiment.py``) produces a label + risk +
``decision_factors``. That's enough to triage but not enough to brief.
This module turns the structured output into a 2-4 sentence Portuguese
narrative summary, gated on ``sentiment_label == "ALERT"`` so the
LLM bill stays bounded.

Architecture:

  GoldArticle (ALERT) ───► render prompt ───► LLMClient.complete
        │                                          │
        │                                          ▼
        ▼                                  NarrativeResult
  NarrativeCache (GCS, content-addressed)

Cache key pins (content_hash, rule_version, prompt_version) so the
same article reprocessed under the same rules + prompt is a cache hit.

The explainer is best-effort: cache or LLM errors return a
``NarrativeResult(summary=None, cache_hit=False, error=str)``. The
pipeline never raises on explainer failure — the classifier label is
already on the row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from mapear_infra.privacy import RedactionLevel, redact

from mapear_nlp.llm.client import LLMClient, LLMError
from mapear_nlp.narrative_cache import NarrativeCache

PROMPT_VERSION = "narrative_v1"
_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "eval" / "prompts" / "narrative_v1.txt"
)
_MAX_EXCERPT_CHARS = 1200


@dataclass(frozen=True)
class NarrativeResult:
    """Outcome of one explainer call.

    ``summary`` is None when the row is not eligible (non-ALERT) OR the
    LLM call failed. The pipeline persists None and moves on.
    ``cache_hit`` and ``error`` are for observability + tests.
    ``redaction_counts`` is the Eixo 6 light audit signal — caller logs
    these for compliance review.
    """

    summary: str | None
    prompt_version: str
    cache_hit: bool = False
    error: str | None = None
    redaction_level: str = "masked"
    redaction_counts: dict[str, int] | None = None


def _load_prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _render_decision_factors(factors: list[dict]) -> str:
    if not factors:
        return "  (no decision factors recorded)"
    lines = []
    for f in factors:
        name = f.get("name", "?")
        value = f.get("value", "?")
        weight = f.get("weight", "?")
        lines.append(f"  - {name}: value={value}, weight={weight}")
    return "\n".join(lines)


def _render_prompt(
    *,
    title: str,
    content: str,
    person_name: str,
    person_role: str,
    polarity: float,
    velocity: float,
    volume: int,
    decision_factors: list[dict],
    template: str,
) -> str:
    excerpt = (content or "")[:_MAX_EXCERPT_CHARS]
    return template.format(
        title=title or "(no title)",
        person_name=person_name or "(unknown)",
        person_role=person_role or "(unknown)",
        polarity=f"{polarity:.2f}",
        velocity=f"{velocity:.2f}",
        volume=volume,
        decision_factors_block=_render_decision_factors(decision_factors),
        content_excerpt=excerpt,
    )


class NarrativeExplainer:
    """Coordinates cache lookup + prompt rendering + LLM call.

    Concrete LLM client and cache are injected so tests can substitute
    in-memory stand-ins. The prompt template is loaded once per
    instance — swap prompts by bumping ``PROMPT_VERSION`` and editing
    ``eval/prompts/narrative_v1.txt`` (which invalidates the cache).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        cache: NarrativeCache | None,
        *,
        max_tokens: int = 200,
        temperature: float = 0.2,
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

    def explain(
        self,
        *,
        content_hash: str,
        title: str,
        content: str,
        person_name: str,
        person_role: str,
        polarity: float,
        velocity: float,
        volume: int,
        decision_factors: list[dict],
        rule_version: str,
    ) -> NarrativeResult:
        """Return a summary for one row. ALERT-gating is the caller's job."""
        cache_key = NarrativeCache.make_key(
            content_hash=content_hash,
            rule_version=rule_version,
            prompt_version=PROMPT_VERSION,
        )

        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None and cached.get("summary"):
                return NarrativeResult(
                    summary=cached["summary"],
                    prompt_version=PROMPT_VERSION,
                    cache_hit=True,
                    redaction_level=self._redaction_level.value,
                    redaction_counts={},
                )

        # Eixo 6 light — redact PII from anything that travels to the
        # external LLM. ``person_name`` is intentionally exempt: public
        # officials acting in office are not PII under LGPD.
        title_red = redact(title, level=self._redaction_level, hmac_key=self._hmac_key)
        content_red = redact(
            content, level=self._redaction_level, hmac_key=self._hmac_key
        )
        # Merge counts so the audit log sees the full picture for this row.
        combined_counts: dict[str, int] = {}
        for cat, n in title_red.counts.items():
            combined_counts[cat] = combined_counts.get(cat, 0) + n
        for cat, n in content_red.counts.items():
            combined_counts[cat] = combined_counts.get(cat, 0) + n

        prompt = _render_prompt(
            title=title_red.text,
            content=content_red.text,
            person_name=person_name,
            person_role=person_role,
            polarity=polarity,
            velocity=velocity,
            volume=volume,
            decision_factors=decision_factors,
            template=self._template,
        )

        try:
            summary = self._llm.complete(
                prompt,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                timeout_seconds=self._timeout,
            )
        except LLMError as exc:
            logger.warning(
                "Narrative LLM call failed for {hash}: {err}",
                hash=content_hash[:12],
                err=exc,
            )
            return NarrativeResult(
                summary=None,
                prompt_version=PROMPT_VERSION,
                error=str(exc),
                redaction_level=self._redaction_level.value,
                redaction_counts=combined_counts,
            )

        if self._cache is not None:
            self._cache.set(
                cache_key,
                {
                    "summary": summary,
                    "prompt_version": PROMPT_VERSION,
                    "rule_version": rule_version,
                },
            )

        return NarrativeResult(
            summary=summary,
            prompt_version=PROMPT_VERSION,
            redaction_level=self._redaction_level.value,
            redaction_counts=combined_counts,
        )
