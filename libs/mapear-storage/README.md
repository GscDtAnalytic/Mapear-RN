# mapear-storage

[← voltar para README raiz](../../README.md)

Loaders e writers para BigQuery, GCS e DuckDB. Idempotência via watermark. Aritmética temporal cross-dialeto.

---

## O que é

A camada que **escreve dados em algum lugar**. Pipelines (RSS, social) e services (NLP runner, graph runner) usam esta lib para:

- Persistir Parquet em GCS (raw/silver/gold zones).
- Carregar tabelas no BigQuery via `LoadJob` (incremental por partição).
- Ler/escrever em DuckDB local (dev/test).
- Manter watermark (`last_processed_at`) por pipeline para idempotência.

Não decide *o quê* escrever — só *como* escrever de forma confiável.

## Por que existe

Os primeiros pipelines do projeto chamavam `bigquery.Client().load_table_from_dataframe(...)` direto, espalhado em vários lugares. Mudanças (ex.: passar a particionar por data, adicionar cluster columns, ligar `enable_list_inference`) exigiam editar N arquivos. Pior: foi exatamente o problema que [causou o incidente de abril/2026](../../README.md#o-incidente-que-moldou-o-projeto) — uma flag faltante em um lugar, e ninguém sabia onde mais essa flag deveria estar.

Centralizar aqui significa: **um único caminho para escrever no BQ**, configurações padronizadas, schemas validados antes do load, e cargas com zero linhas tratadas como erro por padrão.

## Stack

| Tecnologia | Versão | Para quê |
|---|---|---|
| **google-cloud-bigquery** | 3.25 | Cliente BQ, LoadJob, queries. |
| **google-cloud-storage** | 2.18 | GCS upload/download. |
| **PyArrow** | 17.0 | Parquet read/write, in-memory colunar. |
| **pandas** | 2.2 | Manipulação de DataFrames. |
| **Pydantic** | 2.9 | Schemas de tabela como modelos (compilados para BQ Schema). |
| **SQLAlchemy** | 2.0 | Conexão DuckDB e Postgres (metadata). |
| **PyIceberg** | 0.8+ (extra `[iceberg]`) | Catálogo Iceberg para datasets que precisam de time-travel. |

## API pública (essencial)

```python
from mapear_storage.bq import BigQueryWriter
from mapear_storage.gcs import GCSWriter
from mapear_storage.watermark import WatermarkStore

writer = BigQueryWriter.from_settings(settings)
writer.load_dataframe(
    df,
    table="rss_silver.articles",
    write_disposition="WRITE_APPEND",
    partition_field="published_at",
    cluster_fields=["source_type", "municipality_id"],
)

# Idempotência: pipelines avançam o watermark só após carga bem-sucedida
watermark = WatermarkStore.from_settings(settings)
last_run = watermark.get("rss_pipeline")
watermark.advance("rss_pipeline", new_timestamp)
```

## Decisões locais

1. **Schemas BQ versionados em Terraform, não no Python.** O Python valida o DataFrame contra o schema declarado, mas a fonte de verdade é o JSON em [`infra/modules/bigquery/schemas/`](../../infra/modules/bigquery/schemas/). Resultado: schema drift detectável.
2. **Cargas com zero linhas são erro por padrão.** Veio do incidente de abril. Override explícito (`allow_empty=True`) existe para backfills planejados.
3. **GCS como zona de aterrissagem antes do BQ.** Nenhum DataFrame vai direto para o BQ — sempre passa por Parquet em GCS. Permite reprocessamento sem re-ingestão.
4. **Watermark em Postgres (não em arquivo).** Múltiplos workers podem coexistir sem race condition; transação garante consistência.

## Como testar

```bash
uv run pytest libs/mapear-storage/tests/
```

Testes contra DuckDB local. Smoke tests contra BQ rodam em CI com Workload Identity, sob flag `--bq-integration`.

## Estrutura

```
mapear-storage/
├── src/mapear_storage/
│   ├── bq/                    # BigQueryWriter, schema validation, LoadJob wrappers
│   ├── gcs/                   # GCSWriter, Parquet helpers
│   ├── duckdb/                # DuckDB local writer (dev/test)
│   ├── iceberg/               # Iceberg catalog (opcional)
│   ├── watermark.py           # WatermarkStore (Postgres-backed)
│   └── schemas.py             # Pydantic → BQ Schema converters
└── tests/
```
