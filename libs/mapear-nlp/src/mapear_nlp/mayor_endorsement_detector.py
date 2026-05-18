"""Mayor endorsement investigator — Eixo 2 v2d.

Given a mayor and a bundle of recent articles that co-mention the mayor
with one or more gubernatorial candidates, asks the LLM to investigate
whether there is evidence of political alignment toward a candidate.

Unlike the stance classifier (one narrative → favor/contra/neutro), the
endorsement detector reasons over MULTIPLE articles per mayor to reach a
single verdict, so its input grain is the mayor, not the article.

Architecture mirrors the stance classifier (Eixo 2 v2b):

  articles bundle ──► render prompt ──► LLMClient.complete
        │                                      │
        │                                      ▼
        ▼                              EndorsementResult (JSON-parsed)
  NarrativeCache (GCS, content-addressed, separate prefix)

Cache key: the bundle is content-addressed — sha256 over the mayor id and
the sorted set of article ids — so re-running with the same evidence and
prompt is a cache hit. Rotating the prompt by bumping PROMPT_VERSION
invalidates the cache so old verdicts stay queryable while new ones
accumulate under the new key.

The detector is best-effort: JSON parse errors and LLM errors return an
``EndorsementResult(detected_candidate=None, error=...)`` without raising.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from mapear_infra.privacy import RedactionLevel, redact

from mapear_nlp.llm.client import LLMClient, LLMError
from mapear_nlp.narrative_cache import NarrativeCache

PROMPT_VERSION = "endorsement_v1"
_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "eval" / "prompts" / "endorsement_v1.txt"
)
_VALID_CONFIDENCE = frozenset({"alta", "media", "baixa"})
# Sentinel for "no clear alignment" — matches the seed sentinel used by
# dim_rn_cities_mayors.supports_candidate and the dashboard front-end.
NO_ENDORSEMENT = "Indefinido"
# Per-article excerpt cap — keeps the multi-article prompt bounded.
_MAX_EXCERPT_CHARS = 800
# Hard cap on articles per mayor — most recent first; protects prompt size.
_MAX_ARTICLES = 12


@dataclass(frozen=True)
class EndorsementArticle:
    """One piece of evidence fed to the investigator."""

    article_id: str
    title: str
    text: str
    published_at: str = ""
    source: str = ""


@dataclass(frozen=True)
class EndorsementResult:
    """Outcome of one mayor endorsement investigation.

    ``detected_candidate`` is None when the LLM call failed or returned
    unparseable output; it is ``NO_ENDORSEMENT`` when the investigation
    ran but found no clear alignment. ``evidence_ids`` are the article
    ids the LLM cited as supporting its verdict.
    """

    detected_candidate: str | None
    confidence: str | None  # "alta" | "media" | "baixa" | None
    rationale: str | None
    evidence_ids: list[str]
    prompt_version: str
    article_count: int
    cache_hit: bool = False
    error: str | None = None
    redaction_level: str = "masked"
    redaction_counts: dict[str, int] = field(default_factory=dict)


def _load_prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _bundle_hash(mayor_id: str, article_ids: list[str]) -> str:
    """Content-address the evidence bundle for the cache key."""
    payload = mayor_id + "|" + "|".join(sorted(article_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render_articles_block(articles: list[EndorsementArticle]) -> str:
    lines: list[str] = []
    for i, art in enumerate(articles, start=1):
        meta = " · ".join(p for p in (art.published_at, art.source) if p)
        header = f"[{i}]" + (f" ({meta})" if meta else "")
        excerpt = (art.text or "").strip()[:_MAX_EXCERPT_CHARS]
        lines.append(f"{header} {art.title or '(sem título)'}\n    {excerpt}")
    return "\n\n".join(lines)


def _render_prompt(
    *,
    mayor_name: str,
    mayor_party: str,
    candidates: list[str],
    articles: list[EndorsementArticle],
    template: str,
) -> str:
    candidate_list = "\n".join(f"  - {c}" for c in candidates)
    return template.format(
        mayor_name=mayor_name or "(desconhecido)",
        mayor_party=mayor_party or "sem partido",
        candidate_list=candidate_list,
        articles_block=_render_articles_block(articles),
    )


def _parse_endorsement_json(
    raw: str,
    *,
    valid_candidates: frozenset[str],
    articles: list[EndorsementArticle],
) -> tuple[str | None, str | None, str | None, list[str], str | None]:
    """Parse LLM output into (candidate, confidence, rationale, evidence_ids, error).

    Expects: {"candidato", "confianca", "justificativa", "evidencias": [int...]}.
    An unrecognised candidate is coerced to NO_ENDORSEMENT rather than failing —
    the model occasionally returns a near-miss spelling.
    """
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        return None, None, None, [], f"JSON parse error: {exc} — raw={raw[:80]!r}"

    candidate = (parsed.get("candidato") or "").strip()
    if candidate not in valid_candidates and candidate != NO_ENDORSEMENT:
        candidate = NO_ENDORSEMENT

    confidence = parsed.get("confianca")
    if confidence not in _VALID_CONFIDENCE:
        confidence = None

    rationale = (parsed.get("justificativa") or "").strip() or None

    # Map 1-based article numbers cited by the LLM back to article ids.
    evidence_ids: list[str] = []
    for idx in parsed.get("evidencias") or []:
        try:
            pos = int(idx)
        except (TypeError, ValueError):
            continue
        if 1 <= pos <= len(articles):
            evidence_ids.append(articles[pos - 1].article_id)

    return candidate, confidence, rationale, evidence_ids, None


class MayorEndorsementDetector:
    """Cache lookup + prompt rendering + LLM call for one mayor's endorsement.

    The LLM client and cache are injected so tests can substitute in-memory
    stand-ins. Swap prompts by bumping ``PROMPT_VERSION`` and editing
    ``eval/prompts/endorsement_v1.txt`` (which rotates the cache key).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        cache: NarrativeCache | None,
        *,
        max_tokens: int = 600,
        temperature: float = 0.1,
        timeout_seconds: float = 60.0,
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

    def investigate(
        self,
        *,
        mayor_id: str,
        mayor_name: str,
        mayor_party: str,
        candidates: list[str],
        articles: list[EndorsementArticle],
    ) -> EndorsementResult:
        """Return an endorsement verdict for one mayor.

        With no articles there is nothing to investigate — returns
        NO_ENDORSEMENT without an LLM call.
        """
        articles = articles[:_MAX_ARTICLES]
        if not articles:
            return EndorsementResult(
                detected_candidate=NO_ENDORSEMENT,
                confidence=None,
                rationale="Sem artigos co-mencionando o prefeito e um candidato.",
                evidence_ids=[],
                prompt_version=PROMPT_VERSION,
                article_count=0,
                redaction_level=self._redaction_level.value,
            )

        article_ids = [a.article_id for a in articles]
        cache_key = NarrativeCache.make_key(
            content_hash=_bundle_hash(mayor_id, article_ids),
            rule_version="",
            prompt_version=PROMPT_VERSION,
        )

        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None and "detected_candidate" in cached:
                return EndorsementResult(
                    detected_candidate=cached["detected_candidate"],
                    confidence=cached.get("confidence"),
                    rationale=cached.get("rationale"),
                    evidence_ids=cached.get("evidence_ids") or [],
                    prompt_version=PROMPT_VERSION,
                    article_count=len(articles),
                    cache_hit=True,
                    redaction_level=self._redaction_level.value,
                )

        # Eixo 6 light — redact PII before article text leaves the warehouse.
        # Public officials' names are exempt (not PII under LGPD in office).
        counts: dict[str, int] = {}
        redacted: list[EndorsementArticle] = []
        for art in articles:
            red = redact(art.text, level=self._redaction_level, hmac_key=self._hmac_key)
            for k, v in red.counts.items():
                counts[k] = counts.get(k, 0) + v
            redacted.append(
                EndorsementArticle(
                    article_id=art.article_id,
                    title=art.title,
                    text=red.text,
                    published_at=art.published_at,
                    source=art.source,
                )
            )

        prompt = _render_prompt(
            mayor_name=mayor_name,
            mayor_party=mayor_party,
            candidates=candidates,
            articles=redacted,
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
                "Endorsement LLM call failed for {mayor}: {err}",
                mayor=mayor_id,
                err=exc,
            )
            return EndorsementResult(
                detected_candidate=None,
                confidence=None,
                rationale=None,
                evidence_ids=[],
                prompt_version=PROMPT_VERSION,
                article_count=len(articles),
                error=str(exc),
                redaction_level=self._redaction_level.value,
                redaction_counts=counts,
            )

        candidate, confidence, rationale, evidence_ids, parse_error = (
            _parse_endorsement_json(
                raw, valid_candidates=frozenset(candidates), articles=articles
            )
        )

        if parse_error:
            logger.warning(
                "Endorsement parse failed for {mayor}: {err}",
                mayor=mayor_id,
                err=parse_error,
            )
            return EndorsementResult(
                detected_candidate=None,
                confidence=None,
                rationale=None,
                evidence_ids=[],
                prompt_version=PROMPT_VERSION,
                article_count=len(articles),
                error=parse_error,
                redaction_level=self._redaction_level.value,
                redaction_counts=counts,
            )

        if self._cache is not None:
            self._cache.set(
                cache_key,
                {
                    "detected_candidate": candidate,
                    "confidence": confidence,
                    "rationale": rationale,
                    "evidence_ids": evidence_ids,
                },
            )

        return EndorsementResult(
            detected_candidate=candidate,
            confidence=confidence,
            rationale=rationale,
            evidence_ids=evidence_ids,
            prompt_version=PROMPT_VERSION,
            article_count=len(articles),
            cache_hit=False,
            redaction_level=self._redaction_level.value,
            redaction_counts=counts,
        )
