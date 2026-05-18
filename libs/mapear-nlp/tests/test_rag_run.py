"""Integration tests for run_rag CLI — Eixo 2 v2c."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mapear_nlp.rag.run_rag import run

# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeEmbeddingClient:
    model = "test-model"
    dim = 4

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@dataclass
class FakeRow:
    content_hash: str = "hash_abc"
    narrative_summary: str = "Governador cortou saúde."
    distance: float = 0.12
    published_at: object = None
    person_id: object = None
    person_name: object = None
    person_role: object = None
    cluster_id: object = None
    cluster_size: object = None
    cluster_label: object = None
    stance_label: object = None
    stance_confidence: object = None

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


class FakeBQClient:
    def __init__(self, rows):
        self._rows = rows

    def query(self, sql: str):
        return self

    def result(self):
        return self._rows


class FakeLLMClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self, answer: str = "Síntese de teste.") -> None:
        self._answer = answer

    def complete(self, prompt, *, max_tokens, temperature, timeout_seconds):
        return self._answer


# ── tests ─────────────────────────────────────────────────────────────────────


def test_run_returns_answer():
    result = run(
        "consulta de teste",
        region="rn",
        k=3,
        project="proj",
        silver_ds="silver",
        gold_ds="gold",
        embedding_model="test-model",
        bq_client=FakeBQClient([FakeRow()]),
        embedding_client=FakeEmbeddingClient(),
        llm_client=FakeLLMClient(),
    )
    assert result["answer"] == "Síntese de teste."
    assert result["query"] == "consulta de teste"
    assert result["error"] is None


def test_run_writes_json_to_file():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "answer.json"
        run(
            "consulta",
            region=None,
            k=5,
            project="p",
            silver_ds="s",
            gold_ds="g",
            embedding_model="m",
            out_path=out,
            bq_client=FakeBQClient([FakeRow()]),
            embedding_client=FakeEmbeddingClient(),
            llm_client=FakeLLMClient("Resposta escrita."),
        )
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["answer"] == "Resposta escrita."


def test_run_empty_hits_no_llm_call():
    llm = FakeLLMClient()
    called = []
    original = llm.complete

    def spy(*args, **kwargs):
        called.append(True)
        return original(*args, **kwargs)

    llm.complete = spy

    run(
        "q",
        region=None,
        k=5,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
        bq_client=FakeBQClient([]),
        embedding_client=FakeEmbeddingClient(),
        llm_client=llm,
    )
    assert called == []


def test_run_hits_in_output():
    result = run(
        "q",
        region="rn",
        k=2,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
        bq_client=FakeBQClient(
            [FakeRow(), FakeRow(content_hash="hash_xyz", distance=0.3)]
        ),
        embedding_client=FakeEmbeddingClient(),
        llm_client=FakeLLMClient(),
    )
    assert len(result["hits"]) == 2
    assert result["hits"][0]["rank"] == 1
    assert result["hits"][1]["rank"] == 2


def test_run_region_in_output():
    result = run(
        "q",
        region="rn",
        k=5,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
        bq_client=FakeBQClient([]),
        embedding_client=FakeEmbeddingClient(),
        llm_client=FakeLLMClient(),
    )
    assert result["region"] == "rn"
