"""Tests for the LLM narrative explainer (Eixo 2 v1)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mapear_infra.privacy import RedactionLevel

from mapear_nlp.llm.client import LLMError
from mapear_nlp.narrative_cache import NarrativeCache
from mapear_nlp.narrative_explainer import (
    PROMPT_VERSION,
    NarrativeExplainer,
    NarrativeResult,
)


class FakeLLMClient:
    """Records prompts and returns a canned response (or raises)."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, response: str | Exception = "Resumo gerado pelo LLM.") -> None:
        self._response = response
        self.calls: list[tuple[str, int, float, float]] = []

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> str:
        self.calls.append((prompt, max_tokens, temperature, timeout_seconds))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class InMemoryCache:
    """Stand-in for NarrativeCache that lives in a dict."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.gets = 0
        self.sets = 0

    def get(self, key: str) -> dict | None:
        self.gets += 1
        return self.store.get(key)

    def set(self, key: str, payload: dict) -> None:
        self.sets += 1
        self.store[key] = payload


_BASE_KWARGS = dict(
    content_hash="hash_abc",
    title="Governador anuncia corte de R$ 200 mi",
    content="Em coletiva nesta tarde, o governador anunciou cortes...",
    person_name="João da Silva",
    person_role="governador",
    polarity=-0.8,
    velocity=4.5,
    volume=22,
    decision_factors=[
        {"name": "polarity", "value": -0.8, "weight": 0.4, "source": "rule"},
        {"name": "velocity", "value": 4.5, "weight": 0.3, "source": "rule"},
        {"name": "volume", "value": 22, "weight": 0.3, "source": "rule"},
    ],
    rule_version="political-sentiment@2026-04-01",
)


def test_explain_returns_summary_on_first_call_and_caches() -> None:
    llm = FakeLLMClient("Resumo X.")
    cache = InMemoryCache()
    explainer = NarrativeExplainer(llm_client=llm, cache=cache)  # type: ignore[arg-type]

    result = explainer.explain(**_BASE_KWARGS)

    assert isinstance(result, NarrativeResult)
    assert result.summary == "Resumo X."
    assert result.prompt_version == PROMPT_VERSION
    assert result.cache_hit is False
    assert result.error is None
    assert len(llm.calls) == 1
    assert cache.sets == 1


def test_explain_cache_hit_skips_llm() -> None:
    llm = FakeLLMClient("This should never be returned.")
    cache = InMemoryCache()
    key = NarrativeCache.make_key(
        content_hash="hash_abc",
        rule_version="political-sentiment@2026-04-01",
        prompt_version=PROMPT_VERSION,
    )
    cache.store[key] = {"summary": "Resumo cacheado.", "prompt_version": PROMPT_VERSION}

    explainer = NarrativeExplainer(llm_client=llm, cache=cache)  # type: ignore[arg-type]
    result = explainer.explain(**_BASE_KWARGS)

    assert result.summary == "Resumo cacheado."
    assert result.cache_hit is True
    assert len(llm.calls) == 0  # cache short-circuits the LLM
    assert cache.sets == 0


def test_explain_is_idempotent_across_repeated_calls() -> None:
    llm = FakeLLMClient("Resumo Y.")
    cache = InMemoryCache()
    explainer = NarrativeExplainer(llm_client=llm, cache=cache)  # type: ignore[arg-type]

    first = explainer.explain(**_BASE_KWARGS)
    second = explainer.explain(**_BASE_KWARGS)

    assert first.summary == second.summary == "Resumo Y."
    assert second.cache_hit is True
    assert len(llm.calls) == 1  # second call is a cache hit


def test_explain_handles_llm_error_gracefully() -> None:
    llm = FakeLLMClient(LLMError("timeout"))
    cache = InMemoryCache()
    explainer = NarrativeExplainer(llm_client=llm, cache=cache)  # type: ignore[arg-type]

    result = explainer.explain(**_BASE_KWARGS)

    assert result.summary is None
    assert result.error == "timeout"
    assert cache.sets == 0  # don't persist failed runs


def test_explain_runs_without_cache() -> None:
    llm = FakeLLMClient("Sem cache.")
    explainer = NarrativeExplainer(llm_client=llm, cache=None)  # type: ignore[arg-type]

    result = explainer.explain(**_BASE_KWARGS)

    assert result.summary == "Sem cache."
    assert result.cache_hit is False
    assert len(llm.calls) == 1


def test_cache_key_changes_on_rule_version_change() -> None:
    llm = FakeLLMClient("Different rules.")
    cache = InMemoryCache()
    explainer = NarrativeExplainer(llm_client=llm, cache=cache)  # type: ignore[arg-type]

    explainer.explain(**_BASE_KWARGS)
    kwargs_v2 = {**_BASE_KWARGS, "rule_version": "political-sentiment@2026-09-01"}
    explainer.explain(**kwargs_v2)

    # Two distinct rule versions → two cache entries → two LLM calls.
    assert len(llm.calls) == 2
    assert len(cache.store) == 2


def test_prompt_includes_required_fields() -> None:
    llm = FakeLLMClient("ok")
    explainer = NarrativeExplainer(llm_client=llm, cache=None)  # type: ignore[arg-type]
    explainer.explain(**_BASE_KWARGS)
    prompt = llm.calls[0][0]

    assert "Governador anuncia corte" in prompt
    assert "João da Silva" in prompt
    assert "governador" in prompt
    assert "-0.80" in prompt  # polarity formatted
    assert "4.50" in prompt  # velocity formatted
    assert "22" in prompt  # volume
    assert "polarity" in prompt
    assert "velocity" in prompt


def test_narrative_cache_make_key_handles_special_chars() -> None:
    # Rule versions sometimes embed paths or slashes; key must remain a single
    # GCS object name (no nested folders by accident).
    key = NarrativeCache.make_key(
        content_hash="hash_abc",
        rule_version="rev/2026/04",
        prompt_version="narrative_v1",
    )
    assert "/" not in key
    assert key.endswith(".json")


@pytest.mark.parametrize("missing_field", ["title", "person_name", "content"])
def test_explain_tolerates_empty_fields(missing_field: str) -> None:
    llm = FakeLLMClient("Tolera campo vazio.")
    explainer = NarrativeExplainer(llm_client=llm, cache=None)  # type: ignore[arg-type]
    kwargs = {**_BASE_KWARGS, missing_field: ""}
    result = explainer.explain(**kwargs)
    assert result.summary == "Tolera campo vazio."


def test_pii_in_content_is_masked_before_reaching_llm() -> None:
    """Eixo 6 light — the LLM must NOT see raw emails / CPFs / phones."""
    llm = FakeLLMClient("ok")
    explainer = NarrativeExplainer(
        llm_client=llm,
        cache=None,  # type: ignore[arg-type]
        redaction_level=RedactionLevel.MASKED,
    )
    kwargs = {
        **_BASE_KWARGS,
        "title": "Vazamento contendo joao@exemplo.com",
        "content": "O documento listava CPF 123.456.789-09 e telefone (84) 99999-9999.",
    }
    result = explainer.explain(**kwargs)
    prompt = llm.calls[0][0]
    assert "joao@exemplo.com" not in prompt
    assert "123.456.789-09" not in prompt
    assert "99999-9999" not in prompt
    assert "[email]" in prompt
    assert "[cpf]" in prompt
    assert "[phone]" in prompt
    # Audit signal reaches the result.
    assert result.redaction_level == "masked"
    assert result.redaction_counts is not None
    assert result.redaction_counts.get("email") == 1
    assert result.redaction_counts.get("cpf") == 1
    assert result.redaction_counts.get("phone") == 1


def test_pii_redaction_can_be_disabled_with_none_level() -> None:
    llm = FakeLLMClient("ok")
    explainer = NarrativeExplainer(
        llm_client=llm,
        cache=None,  # type: ignore[arg-type]
        redaction_level=RedactionLevel.NONE,
    )
    kwargs = {**_BASE_KWARGS, "content": "Email a@b.com vazou."}
    result = explainer.explain(**kwargs)
    prompt = llm.calls[0][0]
    # NONE level → email flows through to the LLM as-is.
    assert "a@b.com" in prompt
    assert result.redaction_counts == {}


def test_person_name_is_not_redacted() -> None:
    """Public figures named in their public capacity are not LGPD PII."""
    llm = FakeLLMClient("ok")
    explainer = NarrativeExplainer(
        llm_client=llm,
        cache=None,  # type: ignore[arg-type]
        redaction_level=RedactionLevel.MASKED,
    )
    kwargs = {**_BASE_KWARGS, "person_name": "Fátima Bezerra"}
    explainer.explain(**kwargs)
    assert "Fátima Bezerra" in llm.calls[0][0]


def test_set_payload_is_json_serialisable() -> None:
    """The cache.set call uses json.dumps under the hood — the payload
    must survive a round-trip through JSON so future readers can parse it.
    """
    llm = FakeLLMClient("Resumo.")
    cache = InMemoryCache()
    explainer = NarrativeExplainer(llm_client=llm, cache=cache)  # type: ignore[arg-type]
    explainer.explain(**_BASE_KWARGS)
    assert cache.sets == 1
    payload = next(iter(cache.store.values()))
    serialised = json.dumps(payload)
    assert json.loads(serialised) == payload
