"""Unit tests for the RAG retriever — Eixo 2 v2c."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from mapear_nlp.rag.retriever import NarrativeHit, _build_sql, retrieve

# ── Fakes ───────────────────────────────────────────────────────────────────


class FakeEmbeddingClient:
    model = "test-model-v1"
    dim = 4

    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = vector or [0.1, 0.2, 0.3, 0.4]
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self._vector for _ in texts]


@dataclass
class FakeRow:
    content_hash: str
    narrative_summary: str
    distance: float
    published_at: datetime | None = None
    person_id: str | None = None
    person_name: str | None = None
    person_role: str | None = None
    cluster_id: int | None = None
    cluster_size: int | None = None
    cluster_label: str | None = None
    stance_label: str | None = None
    stance_confidence: str | None = None

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


class FakeBQResult:
    def __init__(self, rows: list[FakeRow]) -> None:
        self._rows = rows

    def result(self) -> list[FakeRow]:
        return self._rows


class FakeBQClient:
    def __init__(self, rows: list[FakeRow]) -> None:
        self._rows = rows
        self.last_sql: str = ""

    def query(self, sql: str) -> FakeBQResult:
        self.last_sql = sql
        return FakeBQResult(self._rows)


# ── _build_sql ───────────────────────────────────────────────────────────────


def test_build_sql_contains_model():
    sql = _build_sql(
        project="proj",
        silver_ds="silver",
        gold_ds="gold",
        embedding_model="my-model",
        embedding=[0.1, 0.2],
        region=None,
        k=5,
    )
    assert "my-model" in sql


def test_build_sql_literal_embedding():
    sql = _build_sql(
        project="proj",
        silver_ds="silver",
        gold_ds="gold",
        embedding_model="m",
        embedding=[0.5, -0.3],
        region=None,
        k=5,
    )
    assert "0.5" in sql
    assert "-0.3" in sql


def test_build_sql_top_k():
    sql = _build_sql(
        project="proj",
        silver_ds="silver",
        gold_ds="gold",
        embedding_model="m",
        embedding=[0.1],
        region=None,
        k=7,
    )
    assert "top_k => 7" in sql


def test_build_sql_region_clause():
    sql = _build_sql(
        project="proj",
        silver_ds="silver",
        gold_ds="gold",
        embedding_model="m",
        embedding=[0.1],
        region="rn",
        k=5,
    )
    assert "region = 'rn'" in sql


def test_build_sql_no_region_clause():
    sql = _build_sql(
        project="proj",
        silver_ds="silver",
        gold_ds="gold",
        embedding_model="m",
        embedding=[0.1],
        region=None,
        k=5,
    )
    assert "region = '" not in sql


def test_build_sql_references_datasets():
    sql = _build_sql(
        project="my-proj",
        silver_ds="my_silver",
        gold_ds="my_gold",
        embedding_model="m",
        embedding=[0.1],
        region=None,
        k=5,
    )
    assert "my-proj.my_silver.silver_narrative_embeddings" in sql
    assert "my-proj.my_gold.gold_articles" in sql
    assert "my-proj.my_silver.silver_narrative_clusters" in sql
    assert "my-proj.my_silver.silver_article_stances" in sql


def test_build_sql_cosine_distance_type():
    sql = _build_sql(
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
        embedding=[0.1],
        region=None,
        k=5,
    )
    assert "distance_type => 'COSINE'" in sql


# ── retrieve ─────────────────────────────────────────────────────────────────


def _make_row(**kwargs) -> FakeRow:
    defaults = dict(
        content_hash="abc123",
        narrative_summary="Prefeito cortou verbas da saúde.",
        distance=0.15,
    )
    defaults.update(kwargs)
    return FakeRow(**defaults)


def test_retrieve_returns_hits():
    rows = [_make_row(), _make_row(content_hash="def456", distance=0.25)]
    client = FakeEmbeddingClient()
    bq = FakeBQClient(rows)
    hits = retrieve(
        "quais cortes foram feitos?",
        embedding_client=client,
        bq_client=bq,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
        k=5,
    )
    assert len(hits) == 2
    assert all(isinstance(h, NarrativeHit) for h in hits)


def test_retrieve_empty_result():
    client = FakeEmbeddingClient()
    bq = FakeBQClient([])
    hits = retrieve(
        "consulta sem resultado",
        embedding_client=client,
        bq_client=bq,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
    )
    assert hits == []


def test_retrieve_hit_fields_populated():
    published = datetime(2026, 3, 15, tzinfo=UTC)
    row = _make_row(
        published_at=published,
        person_name="João Silva",
        person_role="prefeito",
        cluster_id=3,
        cluster_size=7,
        stance_label="contra",
        stance_confidence="high",
    )
    bq = FakeBQClient([row])
    hits = retrieve(
        "q",
        embedding_client=FakeEmbeddingClient(),
        bq_client=bq,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
    )
    h = hits[0]
    assert h.published_at == published
    assert h.person_name == "João Silva"
    assert h.person_role == "prefeito"
    assert h.cluster_id == 3
    assert h.cluster_size == 7
    assert h.stance_label == "contra"
    assert h.stance_confidence == "high"


def test_retrieve_distance_is_float():
    row = _make_row(distance=0.123)
    bq = FakeBQClient([row])
    hits = retrieve(
        "q",
        embedding_client=FakeEmbeddingClient(),
        bq_client=bq,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
    )
    assert isinstance(hits[0].distance, float)


def test_retrieve_calls_embedding_once():
    client = FakeEmbeddingClient()
    bq = FakeBQClient([])
    retrieve(
        "test query",
        embedding_client=client,
        bq_client=bq,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
    )
    assert len(client.calls) == 1
    assert client.calls[0] == ["test query"]


def test_retrieve_region_in_sql():
    client = FakeEmbeddingClient()
    bq = FakeBQClient([])
    retrieve(
        "q",
        embedding_client=client,
        bq_client=bq,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
        region="rn",
    )
    assert "region = 'rn'" in bq.last_sql


def test_retrieve_no_region_in_sql():
    client = FakeEmbeddingClient()
    bq = FakeBQClient([])
    retrieve(
        "q",
        embedding_client=client,
        bq_client=bq,
        project="p",
        silver_ds="s",
        gold_ds="g",
        embedding_model="m",
        region=None,
    )
    assert "region = '" not in bq.last_sql
