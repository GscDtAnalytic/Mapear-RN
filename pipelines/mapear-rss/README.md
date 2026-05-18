# mapear-rss

[← voltar para README raiz](../../README.md)

Pipeline ETL que coleta, extrai, enriquece e carrega artigos de portais regionais do RN a cada 8 horas.

---

## O que é

O primeiro pipeline em produção do Mapear-RN. Faz quatro coisas, na ordem:

1. **Descobre** feeds RSS de uma lista mantida em config + descoberta automática (sitemap, autodetect).
2. **Extrai** o conteúdo completo de cada artigo (HTML → texto limpo) com `trafilatura`.
3. **Enriquece** com `mapear-nlp`: NER, entity linking ao domínio RN, sentimento por entidade, tópicos.
4. **Carrega** Parquet em GCS (`rss_raw`, `rss_silver`) e tabelas no BigQuery via `mapear-storage`.

Cada execução roda em ~2-5 minutos como Cloud Run Job, disparado por Cloud Scheduler com `0 */8 * * *`.

## Por que existe (e por que RSS primeiro)

Portais regionais do interior do RN são a fonte mais estável e rica de cobertura municipal. Feeds RSS são:

- **Públicos** (sem ToS impeditivo).
- **Estruturados** (pelo menos o `<item>` é).
- **Cumulativos** (publicações ficam no feed por horas/dias).

Começar por RSS permitiu validar toda a arquitetura (medallion, observabilidade, dbt) com uma fonte previsível antes de partir para redes sociais (mais hostis a scraping).

## Stack local

| Tecnologia | Versão | Para quê |
|---|---|---|
| **feedparser** | 6.0 | Parsing de RSS/Atom. |
| **trafilatura** | 1.8.1 | Extração de conteúdo do HTML. |
| **lxml** + **beautifulsoup4** | 4.9 / 4.12 | Parsing HTML quando trafilatura falha. |
| **readability-lxml** | 0.8 | Fallback secundário. |
| **httpx** | 0.27 | HTTP async, timeout configurável, HTTP/2. |
| **playwright** + **playwright-stealth** | 1.45 / 1.0 | Headless browser para portais com anti-bot. **Opt-in por feed** — não roda em todos. |
| **mapear-domain / -infra / -nlp / -storage** | — | Toda a base do workspace. |

## Como rodar

```bash
# Local (target dev — DuckDB)
ENVIRONMENT=local uv run python -m mapear_rss

# Com configuração específica
ENVIRONMENT=local FEEDS_FILE=./config/feeds.yaml uv run python -m mapear_rss
```

Em produção, o container é construído pelo workflow `cd-build-push.yml`, publicado no Artifact Registry, e o Cloud Run Job é atualizado pelo Terraform.

## Decisões locais

1. **Watermark por feed, não por pipeline.** Cada feed avança independentemente. Um feed quebrado não impede os outros de progredir.
2. **Trafilatura como extrator primário.** Avaliação no golden set de 200 artigos em PT-BR mostrou trafilatura > readability + soup em qualidade de texto extraído. *Trade-off: dependência mais pesada; aceito.*
3. **Playwright opt-in.** Apenas feeds explicitamente marcados em config usam headless browser. Custo de cold-start (~3-5s por artigo) só justifica para portais que bloqueiam HTTP simples.
4. **Circuit breaker por portal.** Feed que falhou N vezes nos últimos M minutos é skippado por um período — evita perder o job inteiro por causa de um portal flaky.
5. **Schema BQ vinculado ao Terraform.** A tabela `rss_silver.articles` é recurso Terraform; mudanças passam por PR. Veio do incidente de abril.

## Estrutura

```
mapear-rss/
├── src/mapear_rss/
│   ├── __main__.py            # Entry point (`python -m mapear_rss`)
│   ├── discovery/             # Descoberta de feeds (sitemap, autodetect)
│   ├── extraction/            # trafilatura + fallbacks + playwright opcional
│   ├── enrichment/            # Chama mapear-nlp e formata para BQ
│   ├── loaders/               # GCS + BQ via mapear-storage
│   ├── config.py              # RSSSettings extends mapear_infra.Settings
│   └── pipeline.py            # Orquestra discovery → extract → enrich → load
├── tests/
└── pyproject.toml
```

## Como testar

```bash
uv run pytest pipelines/mapear-rss/tests/
```

Testes usam `respx` para mockar HTTP — o pipeline é testado contra fixtures de feeds reais arquivados em `tests/fixtures/`.

## Schedule em produção

`0 */8 * * *` (a cada 8 horas, UTC). Configurado em [`infra/modules/cloud_scheduler/`](../../infra/modules/cloud_scheduler/).
