# dbt — projeto `mapear_rn`

[← voltar para README raiz](../README.md)

Transformação SQL em camadas (medallion): `staging` → `intermediate` → `marts`. Mesmo modelo roda em DuckDB (dev) e BigQuery (prod).

---

## O que é

O projeto dbt central do Mapear-RN. Transforma o dado bruto carregado pelos pipelines em:

- **`staging/`** — modelos `stg_<fonte>__<entidade>` que limpam, tipam e deduplicam. Um por fonte (`stg_rss__articles`, `stg_social__posts`).
- **`intermediate/`** — modelos `int_<dominio>__<descrição>` que unem fontes (`int_articles__rn_enriched` une RSS + Social), resolvem identidades e materializam joins pesados.
- **`marts/`** — `fct_*` (fatos) e `dim_*` (dimensões), o que o dashboard e analistas consomem.

## Por que dbt

Antes do dbt, transformações viviam em Python como `.sql` strings ou queries inline. Custo de manutenção alto:

- Sem testes nativos.
- Sem lineage (quem depende de quem).
- Sem docs geradas.
- Sem macros (lógica replicada em N modelos).

dbt resolve isso e é **padrão de mercado** — qualquer analista que entra no projeto não precisa aprender ferramenta proprietária.

## Stack

| Tecnologia | Versão | Para quê |
|---|---|---|
| **dbt-core** | 1.11.8 | Engine de transformações. |
| **dbt-bigquery** | 1.11.1 | Adapter para o target de produção. |
| **dbt-duckdb** | 1.10.1 | Adapter para dev local (DuckDB embarcado). |
| **dbt_utils** | (pacote dbt) | Macros utilitárias. |
| **sqlfluff** | 3.1+ | Linter SQL (rodado em pre-commit e CI). |

## Targets

| Target | Database | Onde usa |
|---|---|---|
| **dev** | DuckDB (`../data/mapear_rn.duckdb`) | Desenvolvimento local. Zero custo, zero internet. |
| **prod** | BigQuery (projeto GCP) | Produção. Particionado + clusterizado. |

O **mesmo SQL** compila para os dois targets. Macros do dbt (`{{ dbt.dateadd(...) }}`) e configurações por target em `dbt_project.yml` cuidam das diferenças de dialeto.

> **Regra de ouro:** nunca usar sintaxe dialeto-específica em modelos. Pre-commit hook bloqueia construções como `INTERVAL '1 day'` (DuckDB-only). Para aritmética temporal, sempre macros dbt ou `TIMESTAMP_ADD(..., INTERVAL N UNIT)` (válido em ambos).

## Sources

- `rss_raw`, `rss_silver`, `rss_gold` — datasets carregados pelo `mapear-rss`
- `social_raw`, `social_silver`, `social_gold` — carregados pelo `mapear-social`
- `youtube_raw`, `youtube_silver`, `youtube_gold` — reservados (pipeline planejado)

`source()` só aparece em modelos de `staging/`. Todos os outros usam `ref()`.

## Modelos principais

```
staging/
├── rss/
│   ├── stg_rss__articles.sql
│   └── stg_rss__feed_metrics.sql
└── social/
    └── stg_social__posts.sql

intermediate/
├── int_articles__rn_enriched.sql      # Union RSS + Social, resolve identidades
├── int_persons__resolved.sql          # Resolução canônica de pessoas
└── int_topics__hierarchy.sql

marts/
├── fct_content.sql                    # Tabela fato principal (substitui fct_articles)
├── fct_entity_sentiment.sql
├── fct_narrative_cluster_*.sql        # Eixo 2 (narrativas)
├── fct_author_persona_daily.sql
├── fct_author_community_*.sql         # Eixo 3 (grafos)
├── dim_rn_cities_mayors.sql           # 167 municípios, prefeitos atuais
├── dim_persons.sql
├── dim_topics.sql
└── dim_sources.sql
```

## Convenções

- **Nomenclatura:** `stg_<fonte>__<entidade>`, `int_<dominio>__<descrição>`, `fct_<fato>`, `dim_<dimensão>`.
- **Idioma:** SQL em UPPERCASE para keywords; identificadores em snake_case.
- **`source()` only in staging.** `ref()` em todo o resto. Lineage limpo.
- **Coluna `source_type` em todo conteúdo** — `rss`, `social_facebook`, `social_instagram`, etc. Habilita queries cross-source.

## Seeds

[`seeds/rn_cities_mayors.csv`](seeds/rn_cities_mayors.csv) — **fonte de verdade dos 167 municípios e prefeitos**. Nunca hardcode esses dados em Python ou SQL; sempre referenciar via `mapear-domain.rn_entities` ou o modelo `dim_rn_cities_mayors`.

## Testes

```bash
make dbt-build     # seed + run + test
make dbt-seed      # só seeds
make dbt-run       # só modelos
make dbt-test      # só testes
```

**Tipos de teste:**

- **Schema tests** (em `schema.yml`) — `not_null`, `unique`, `relationships`, `accepted_values`.
- **Singular tests** (em `tests/singular/`) — 20+ assertivas SQL específicas: ex. "todos os artigos têm pelo menos uma entidade resolvida com confiança > 0.5".
- **Drift tests parametrizados** — comparam schema atual vs schema esperado por tabela crítica.

Total: **35 campos derivados** monitorados com **39 métricas de qualidade**. Baselines medidos em produção, não inventados ([`docs/sprint3_b4_baseline_plan.md`](../docs/sprint3_b4_baseline_plan.md)).

## Decisões locais

1. **Medallion explícito.** Camadas separadas permitem reprocessamento granular (só `int_*` se o NLP mudou; só `fct_*` se a agregação mudou).
2. **Materialização agressiva em marts.** `fct_*` são tabelas (não views) — particionadas por data, clusterizadas por município. Custo de query no dashboard fica < R$ 0,01 por visualização.
3. **Schema BQ versionado em Terraform.** dbt **não** cria tabelas raw em produção — quem cria é Terraform. dbt só lê delas. Evita drift entre o que o pipeline grava e o que o dbt espera.
4. **dbt_packages/ no .gitignore.** `dbt deps` reconstrói em CI. Lock file (`package-lock.yml`) é commitado.

## Estrutura

```
dbt/
├── dbt_project.yml          # Profiles, targets, materializations
├── profiles.yml             # Dev (DuckDB) + Prod (BigQuery)
├── models/
│   ├── staging/             # stg_<fonte>__<entidade>
│   ├── intermediate/        # int_<dominio>__<descrição>
│   └── marts/               # fct_* / dim_*
├── seeds/                   # CSVs versionados (rn_cities_mayors.csv)
├── tests/
│   └── singular/            # 20+ assertivas SQL
├── macros/                  # Macros custom + helpers
└── packages.yml             # dbt_utils etc
```
