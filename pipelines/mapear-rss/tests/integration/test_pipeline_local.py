"""Integration tests for the local pipeline flow.

These tests validate the data flow between pipeline stages
without external dependencies (no Postgres, no Redis).
They use in-memory structures and local filesystem.
"""

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from mapear_domain.models.base import RawArticle
from mapear_nlp.ner import NERExtractor
from mapear_nlp.transformation.cleaner import clean_text
from mapear_rss.extraction.content_hasher import hash_content
from mapear_rss.transformation.deduplicator import Deduplicator


@pytest.fixture
def temp_lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary data lake directory."""
    lake = tmp_path / "lake"
    (lake / "raw").mkdir(parents=True)
    (lake / "silver").mkdir(parents=True)
    (lake / "gold").mkdir(parents=True)
    monkeypatch.setenv("DATA_LAKE_PATH", str(lake))
    return lake


@pytest.fixture
def sample_raw_articles() -> list[RawArticle]:
    """Create sample raw articles using synthetic test Region entities."""
    return [
        RawArticle(
            url="https://diariofake.com.br/noticia-1",
            source_feed="https://diariofake.com.br/feed/",
            title="Prefeito de Testópolis anuncia novo hospital",
            content=(
                "O prefeito João Teste anunciou nesta terça-feira a construção "
                "de um novo hospital em Testópolis. "
                "O investimento será de R$ 50 milhões "
                "com recursos federais destinados à saúde pública do estado. "
                "A obra deve começar no primeiro semestre de 2027 e beneficiar "
                "mais de 100 mil habitantes da cidade."
            ),
            author="Repórter Fake",
            published_at=datetime.now(UTC),
            content_hash=hash_content(
                "Prefeito de Testópolis anuncia novo hospital",
                "O prefeito João Teste anunciou construção de um novo hospital",
            ),
        ),
        RawArticle(
            url="https://portalsim.com.br/noticia-2",
            source_feed="https://portalsim.com.br/rss2.xml",
            title="Vilafake investe em educação básica",
            content=(
                "A prefeita Maria Fake inaugurou três novas creches em Vilafake "
                "nesta quarta-feira. As unidades fazem parte do programa municipal de "
                "expansão da educação infantil, que prevê 10 novas escolas até 2027. "
                "O investimento total é de R$ 15 milhões do orçamento municipal."
            ),
            author="Portal Simulado",
            published_at=datetime.now(UTC),
            content_hash=hash_content(
                "Vilafake investe em educação básica",
                "A prefeita Maria Fake inaugurou três novas creches em Vilafake",
            ),
        ),
        RawArticle(
            url="https://example.com/noticia-irrelevante",
            source_feed="https://uol.com.br/rss",
            title="São Paulo registra queda na Bovespa",
            content=(
                "O índice Ibovespa fechou em queda de 1,2% nesta terça-feira, "
                "pressionado por resultados trimestrais abaixo do esperado "
                "de grandes bancos. Analistas preveem volatilidade para a semana."
            ),
            author="UOL Economia",
            published_at=datetime.now(UTC),
            content_hash=hash_content(
                "São Paulo registra queda na Bovespa",
                "O índice Ibovespa fechou em queda",
            ),
        ),
    ]


class TestPipelineDataFlow:
    """Test the data flow from Raw → Silver."""

    def test_deduplication_removes_copies(
        self, sample_raw_articles: list[RawArticle]
    ) -> None:
        # Adicionar duplicata
        duplicate = sample_raw_articles[0].model_copy()
        articles = sample_raw_articles + [duplicate]

        dedup = Deduplicator()
        unique = dedup.deduplicate(articles)

        assert len(unique) == 3  # 3 únicos, 1 duplicata removida

    def test_ner_flags_rn_relevant(
        self, sample_raw_articles: list[RawArticle], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ENRICHMENT_MODE", "skip")

        ner = NERExtractor()
        silver = ner.extract_batch(sample_raw_articles)

        assert len(silver) == 3

        # Artigo sobre Testópolis — deve ser relevant
        testopolis_article = [s for s in silver if "Testópolis" in s.title][0]
        assert testopolis_article.is_rn_relevant is True
        assert "Testópolis" in testopolis_article.mentioned_cities

        # Artigo sobre Vilafake — deve ser relevant
        vilafake_article = [s for s in silver if "Vilafake" in s.title][0]
        assert vilafake_article.is_rn_relevant is True
        assert "Vilafake" in vilafake_article.mentioned_cities

        # Artigo sobre Bovespa — NÃO deve ser RN-relevant
        irrelevant = [s for s in silver if "Bovespa" in s.title][0]
        assert irrelevant.is_rn_relevant is False

    def test_ner_no_false_positive_substring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'natal' inside 'neonatal' or 'prenatal' should NOT match."""
        monkeypatch.setenv("ENRICHMENT_MODE", "skip")

        article = RawArticle(
            url="https://example.com/neonatal",
            source_feed="https://example.com/feed",
            title="Hospital inaugura UTI neonatal em São Paulo",
            content=(
                "O hospital inaugurou a nova UTI neonatal com 30 leitos. "
                "O atendimento prenatal também será ampliado com recursos "
                "do governo federal destinados à saúde pública nacional. "
                "A expectativa é atender mais de 200 pacientes por mês."
            ),
            published_at=datetime.now(UTC),
            content_hash=hash_content(
                "Hospital inaugura UTI neonatal",
                "UTI neonatal com 30 leitos",
            ),
        )

        ner = NERExtractor()
        silver = ner.extract(article)

        assert silver.is_rn_relevant is False
        assert "Testópolis" not in silver.mentioned_cities

    def test_text_cleaner_removes_boilerplate(self) -> None:
        dirty = (
            "O prefeito inaugurou a obra nesta manhã em Testópolis.\n"
            "Leia também: Governo libera verba para saneamento\n"
            "Siga o portal no Instagram @portalsimulado\n"
            "A obra deve ficar pronta até o final do ano "
            "e beneficiar mais de 100 mil pessoas."
        )
        clean = clean_text(dirty)
        assert "Leia também" not in clean
        assert "Instagram" not in clean
        assert "inaugurou" in clean

    def test_parquet_write_and_read(
        self, temp_lake: Path, sample_raw_articles: list[RawArticle]
    ) -> None:
        df = pd.DataFrame([a.model_dump(mode="json") for a in sample_raw_articles])

        # Escrever
        raw_dir = temp_lake / "raw" / "batch=test"
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / "data.parquet"
        df.to_parquet(path, engine="pyarrow", compression="snappy")

        # Ler e validar
        df_read = pd.read_parquet(path)
        assert len(df_read) == 3
        assert "content_hash" in df_read.columns
        assert "url" in df_read.columns

    def test_full_raw_to_silver_flow(
        self,
        sample_raw_articles: list[RawArticle],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: Raw → Dedup → NER → Silver."""
        monkeypatch.setenv("ENRICHMENT_MODE", "skip")

        # Dedup
        dedup = Deduplicator()
        unique = dedup.deduplicate(sample_raw_articles)
        assert len(unique) == 3

        # NER
        ner = NERExtractor()
        silver = ner.extract_batch(unique)

        # Validar
        rn_relevant = [s for s in silver if s.is_rn_relevant]
        non_relevant = [s for s in silver if not s.is_rn_relevant]

        assert len(rn_relevant) == 2  # Testópolis + Vilafake
        assert len(non_relevant) == 1  # Bovespa

        # Serializar para Parquet
        df = pd.DataFrame([s.model_dump(mode="json") for s in silver])
        assert "is_rn_relevant" in df.columns
        assert "mentioned_cities" in df.columns
        assert df["is_rn_relevant"].sum() == 2
