"""Unit tests for the RAG generator — Eixo 2 v2c."""

from __future__ import annotations

from datetime import UTC, datetime

from mapear_nlp.llm.client import LLMError
from mapear_nlp.rag.generator import RAGAnswer, _format_hit, generate
from mapear_nlp.rag.retriever import NarrativeHit

# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeLLMClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self, response: str | Exception = "Resposta sintetizada.") -> None:
        self._response = response
        self.prompts: list[str] = []

    def complete(self, prompt, *, max_tokens, temperature, timeout_seconds):
        self.prompts.append(prompt)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _hit(**kwargs) -> NarrativeHit:
    defaults = dict(
        content_hash="abc123",
        narrative_summary="Prefeito cortou verbas da educação.",
        distance=0.1,
    )
    defaults.update(kwargs)
    return NarrativeHit(**defaults)


# ── _format_hit ───────────────────────────────────────────────────────────────


def test_format_hit_basic():
    h = _hit()
    text = _format_hit(1, h)
    assert "[1]" in text
    assert "Prefeito cortou verbas" in text
    assert "data desconhecida" in text


def test_format_hit_with_date():
    h = _hit(published_at=datetime(2026, 4, 10, tzinfo=UTC))
    text = _format_hit(2, h)
    assert "2026-04-10" in text


def test_format_hit_with_person():
    h = _hit(person_name="Maria Lima", person_role="governadora")
    text = _format_hit(1, h)
    assert "Maria Lima" in text
    assert "governadora" in text


def test_format_hit_with_stance():
    h = _hit(stance_label="contra", stance_confidence="high")
    text = _format_hit(1, h)
    assert "contra" in text
    assert "high" in text


def test_format_hit_with_cluster():
    h = _hit(cluster_id=5, cluster_size=12)
    text = _format_hit(1, h)
    assert "cluster #5" in text
    assert "12 membros" in text


def test_format_hit_outlier_cluster_not_shown():
    h = _hit(cluster_id=-1)
    text = _format_hit(1, h)
    assert "cluster" not in text


def test_format_hit_no_metadata():
    h = _hit()
    text = _format_hit(1, h)
    assert "alvo:" not in text
    assert "posicionamento:" not in text
    assert "cluster" not in text


# ── generate ─────────────────────────────────────────────────────────────────


def test_generate_returns_answer():
    llm = FakeLLMClient("Síntese em português.")
    hits = [_hit()]
    result = generate("consulta", hits, llm_client=llm)
    assert isinstance(result, RAGAnswer)
    assert result.answer == "Síntese em português."
    assert result.error is None


def test_generate_empty_hits_returns_no_results():
    llm = FakeLLMClient()
    result = generate("consulta", [], llm_client=llm)
    assert "Nenhuma narrativa" in result.answer
    assert result.error is None
    assert llm.prompts == []


def test_generate_prompt_contains_query():
    llm = FakeLLMClient("ok")
    generate("minha consulta especial", [_hit()], llm_client=llm)
    assert "minha consulta especial" in llm.prompts[0]


def test_generate_prompt_contains_narrative_summary():
    llm = FakeLLMClient("ok")
    hits = [_hit(narrative_summary="Texto da narrativa de teste.")]
    generate("q", hits, llm_client=llm)
    assert "Texto da narrativa de teste." in llm.prompts[0]


def test_generate_llm_error_captured():
    llm = FakeLLMClient(LLMError("timeout"))
    result = generate("q", [_hit()], llm_client=llm)
    assert result.answer == ""
    assert result.error is not None
    assert "timeout" in result.error


def test_generate_k_matches_hits():
    llm = FakeLLMClient("ok")
    hits = [_hit(), _hit(content_hash="x2"), _hit(content_hash="x3")]
    result = generate("q", hits, llm_client=llm)
    assert result.k == 3


def test_generate_model_from_client():
    llm = FakeLLMClient("ok")
    result = generate("q", [_hit()], llm_client=llm)
    assert result.model == "fake-model"


def test_generate_region_carried():
    llm = FakeLLMClient("ok")
    result = generate("q", [_hit()], llm_client=llm, region="rn")
    assert result.region == "rn"


def test_generate_generated_at_set():
    llm = FakeLLMClient("ok")
    result = generate("q", [_hit()], llm_client=llm)
    assert result.generated_at is not None
    assert result.generated_at.tzinfo is not None
