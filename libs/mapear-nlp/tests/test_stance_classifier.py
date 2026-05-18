"""Unit tests for the stance classifier — Eixo 2 v2b."""

from __future__ import annotations

from typing import Any

from mapear_infra.privacy import RedactionLevel

from mapear_nlp.llm.client import LLMError
from mapear_nlp.stance_classifier import (
    PROMPT_VERSION,
    StanceClassifier,
    _parse_stance_json,
)


class FakeLLMClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self, response: str | Exception = "") -> None:
        self._response = response
        self.calls: list[tuple[str, int, float, float]] = []

    def complete(self, prompt, *, max_tokens, temperature, timeout_seconds):
        self.calls.append((prompt, max_tokens, temperature, timeout_seconds))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class InMemoryCache:
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


_BASE = dict(
    content_hash="hash_abc",
    narrative_summary="O prefeito anunciou corte de R$ 200 mi na saúde pública.",
    person_name="João Silva",
    person_role="prefeito",
    rule_version="political-sentiment@2026-04-01",
)


# --- _parse_stance_json ---


def test_parse_json_favor():
    stance, conf, err = _parse_stance_json('{"stance": "favor", "confidence": "high"}')
    assert stance == "favor"
    assert conf == "high"
    assert err is None


def test_parse_json_contra():
    stance, conf, err = _parse_stance_json(
        '{"stance": "contra", "confidence": "medium"}'
    )
    assert stance == "contra"
    assert err is None


def test_parse_json_neutro():
    stance, conf, err = _parse_stance_json('{"stance": "neutro", "confidence": "low"}')
    assert stance == "neutro"
    assert conf == "low"
    assert err is None


def test_parse_json_invalid_json_returns_error():
    stance, conf, err = _parse_stance_json("not json")
    assert stance is None
    assert err is not None
    assert "JSON parse error" in err


def test_parse_json_unknown_stance_returns_error():
    stance, conf, err = _parse_stance_json(
        '{"stance": "positivo", "confidence": "high"}'
    )
    assert stance is None
    assert err is not None
    assert "positivo" in err


def test_parse_json_missing_confidence_is_tolerated():
    stance, conf, err = _parse_stance_json('{"stance": "neutro"}')
    assert stance == "neutro"
    assert conf is None
    assert err is None


def test_parse_json_unexpected_confidence_yields_none():
    stance, conf, err = _parse_stance_json(
        '{"stance": "favor", "confidence": "very-high"}'
    )
    assert stance == "favor"
    assert conf is None  # tolerated — not an error
    assert err is None


# --- StanceClassifier ---


def test_classify_returns_stance_on_success():
    llm = FakeLLMClient('{"stance": "contra", "confidence": "high"}')
    cache = InMemoryCache()
    clf = StanceClassifier(llm, cache)
    result = clf.classify(**_BASE)
    assert result.stance_label == "contra"
    assert result.confidence == "high"
    assert result.cache_hit is False
    assert result.error is None
    assert result.prompt_version == PROMPT_VERSION


def test_classify_caches_result_after_llm_call():
    llm = FakeLLMClient('{"stance": "favor", "confidence": "medium"}')
    cache = InMemoryCache()
    clf = StanceClassifier(llm, cache)
    clf.classify(**_BASE)
    assert cache.sets == 1
    cached = list(cache.store.values())[0]
    assert cached["stance_label"] == "favor"
    assert cached["prompt_version"] == PROMPT_VERSION


def test_classify_cache_hit_skips_llm():
    llm = FakeLLMClient('{"stance": "contra", "confidence": "high"}')
    cache = InMemoryCache()
    clf = StanceClassifier(llm, cache)
    # prime the cache
    clf.classify(**_BASE)
    assert len(llm.calls) == 1
    # second call → cache hit, same label returned
    result = clf.classify(**_BASE)
    assert len(llm.calls) == 1  # no new LLM call
    assert result.cache_hit is True
    assert result.stance_label == "contra"


def test_classify_cache_hit_returns_cached_stance():
    llm = FakeLLMClient('{"stance": "neutro", "confidence": "low"}')
    cache = InMemoryCache()
    clf = StanceClassifier(llm, cache)
    clf.classify(**_BASE)
    result = clf.classify(**_BASE)
    assert result.stance_label == "neutro"
    assert result.cache_hit is True


def test_classify_llm_error_returns_error_result():
    llm = FakeLLMClient(LLMError("timeout"))
    clf = StanceClassifier(llm, cache=None)
    result = clf.classify(**_BASE)
    assert result.stance_label is None
    assert result.error is not None
    assert "timeout" in result.error


def test_classify_json_parse_error_returns_error_result():
    llm = FakeLLMClient("Here is my analysis: the stance is positive.")
    clf = StanceClassifier(llm, cache=None)
    result = clf.classify(**_BASE)
    assert result.stance_label is None
    assert result.error is not None


def test_classify_no_cache_still_works():
    llm = FakeLLMClient('{"stance": "favor", "confidence": "high"}')
    clf = StanceClassifier(llm, cache=None)
    result = clf.classify(**_BASE)
    assert result.stance_label == "favor"
    assert result.cache_hit is False


def test_classify_applies_pii_redaction():
    llm = FakeLLMClient('{"stance": "neutro", "confidence": "high"}')
    clf = StanceClassifier(llm, cache=None, redaction_level=RedactionLevel.MASKED)
    kwargs = dict(
        _BASE, narrative_summary="CPF 123.456.789-00 foi citado no relatório."
    )
    result = clf.classify(**kwargs)
    prompt_sent = llm.calls[0][0]
    assert "123.456.789-00" not in prompt_sent
    assert "[cpf]" in prompt_sent
    assert result.redaction_level == "masked"


def test_classify_person_name_not_redacted():
    llm = FakeLLMClient('{"stance": "favor", "confidence": "high"}')
    clf = StanceClassifier(llm, cache=None, redaction_level=RedactionLevel.MASKED)
    result = clf.classify(**_BASE)
    prompt_sent = llm.calls[0][0]
    # person_name should appear in the prompt unchanged
    assert "João Silva" in prompt_sent
    assert result.stance_label == "favor"


def test_classify_cache_key_includes_prompt_version():
    llm = FakeLLMClient('{"stance": "contra", "confidence": "low"}')
    cache = InMemoryCache()
    clf = StanceClassifier(llm, cache)
    clf.classify(**_BASE)
    key = list(cache.store.keys())[0]
    assert PROMPT_VERSION in key


def test_classify_prompt_version_in_result():
    llm = FakeLLMClient('{"stance": "neutro", "confidence": "medium"}')
    clf = StanceClassifier(llm, cache=None)
    result = clf.classify(**_BASE)
    assert result.prompt_version == PROMPT_VERSION


def test_classify_uses_configured_max_tokens():
    llm = FakeLLMClient('{"stance": "favor", "confidence": "high"}')
    clf = StanceClassifier(llm, cache=None, max_tokens=42)
    clf.classify(**_BASE)
    _, max_tokens, _, _ = llm.calls[0]
    assert max_tokens == 42


def test_classify_cache_miss_does_not_serve_invalid_cache_entry():
    """A cached entry with stance_label=None must NOT be served as a hit."""
    llm = FakeLLMClient('{"stance": "favor", "confidence": "high"}')
    cache = InMemoryCache()
    # Manually inject a bad cache entry (e.g. from a prior error).
    from mapear_nlp.narrative_cache import NarrativeCache

    key = NarrativeCache.make_key(
        content_hash=_BASE["content_hash"],
        rule_version=_BASE["rule_version"],
        prompt_version=PROMPT_VERSION,
    )
    cache.store[key] = {"stance_label": None, "prompt_version": PROMPT_VERSION}
    clf = StanceClassifier(llm, cache)
    result = clf.classify(**_BASE)
    # Should call LLM (cache entry is invalid) and return a real label.
    assert len(llm.calls) == 1
    assert result.stance_label == "favor"
    assert result.cache_hit is False
