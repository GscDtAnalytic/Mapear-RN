# Mapear-RN

[English](README.md) · **Português**

> Plataforma ETL de monitoramento sócio-político do Rio Grande do Norte. São 167 municípios, multi-fonte, rodando em produção por menos de R$ 5/mês.

[![CI](https://github.com/GscDtAnalytic/Mapear-RN/actions/workflows/ci.yml/badge.svg)](https://github.com/GscDtAnalytic/Mapear-RN/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![uv workspace](https://img.shields.io/badge/uv-workspace-purple)]()
[![dbt 1.11](https://img.shields.io/badge/dbt-1.11.8-orange)]()
[![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC)]()
[![GCP](https://img.shields.io/badge/cloud-GCP-4285F4)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green)]()

## O que é, em uma linha

Um pipeline de dados de produção que coleta o que portais regionais e redes sociais publicam sobre prefeitos, vereadores e narrativas políticas dos 167 municípios do RN, enriquece com NLP em português, modela em um data warehouse e disponibiliza via API e dashboard.

Este README é para tech leads e recrutadores que querem entender, em três a cinco minutos, o que foi construído, com quais tecnologias e por quê. O foco está nos trade-offs de custo-benefício e nas decisões que realmente importaram. O detalhe técnico profundo de cada componente mora nos READMEs dos subprojetos, linkados no final.

## Sumário

- [TL;DR](#tldr)
- [Arquitetura em 30 segundos](#arquitetura-em-30-segundos)
- [Filosofia de engenharia](#filosofia-de-engenharia)
- [Stack técnica](#stack-técnica)
- [Decisões de custo-benefício](#decisões-de-custo-benefício)
- [O incidente que moldou o projeto](#o-incidente-que-moldou-o-projeto)
- [Camadas de dados (medallion)](#camadas-de-dados-medallion)
- [Como rodar localmente](#como-rodar-localmente)
- [Estrutura do repositório](#estrutura-do-repositório)
- [Roadmap e status](#roadmap-e-status)
- [Marcos](#marcos)
- [Licença](#licença)

## TL;DR

| Pergunta | Resposta curta |
|---|---|
| Qual o problema? | A cobertura sócio-política dos 167 municípios do RN é fragmentada entre portais regionais e redes sociais. Não há série temporal nem agregação. |
| Qual a solução? | Coletar (RSS + Apify), padronizar, enriquecer com NLP determinístico em português e modelar com dbt em um warehouse cloud. |
| Quais fontes? | Portais de notícia (RSS) mais Facebook, Instagram, X e TikTok (Apify). YouTube planejado. |
| Onde roda? | Google Cloud Platform. Cloud Run Jobs disparados por Cloud Scheduler, dados em GCS e BigQuery. |
| Quanto custa? | Menos de R$ 5/mês em produção. A mesma arquitetura com Composer/Airflow always-on passaria de R$ 150/mês. |
| Quem consome? | Dashboard React e API FastAPI sobre BigQuery, com modelos dbt prontos para BI. |

## Arquitetura em 30 segundos

```
                ┌───────────────────────────────────────────────────────────────┐
                │  FONTES                                                       │
                │  RSS feeds  •  Facebook / Instagram / X / TikTok (via Apify)  │
                └────────────────────────────┬──────────────────────────────────┘
                                             │
                                             ▼
              ┌──────────────────────────────────────────────────────────┐
              │  INGESTÃO — Cloud Run Jobs (cron via Cloud Scheduler)   │
              │  • mapear-rss      — a cada 8h                          │
              │  • mapear-social   — cadência variável por rede         │
              └────────────────────────────┬─────────────────────────────┘
                                           │
                  ┌────────────────────────┴────────────────────────┐
                  ▼                                                 ▼
        ┌──────────────────┐                            ┌──────────────────────┐
        │  GCS (data lake) │ ──── reprocessamento ────► │  BigQuery (warehouse) │
        │  raw • silver    │                            │  silver • gold • marts │
        │  Parquet         │                            │  particionado +        │
        │                  │                            │  clusterizado          │
        └──────────────────┘                            └──────────┬───────────┘
                                                                   │
                  ┌────────────────────────────────────────────────┘
                  ▼                                                ▼
        ┌──────────────────────────┐                  ┌──────────────────────────┐
        │  dbt (mapear_rn)         │                  │  Cloud Run Services      │
        │  staging → intermediate  │                  │  • freshness-emitter     │
        │  → marts (fct_* / dim_*) │                  │  • alert-runner          │
        └──────────┬───────────────┘                  │  • nlp-runner / graph    │
                   │                                  └──────────────────────────┘
                   ▼
        ┌──────────────────────────────────────┐
        │  Camada de consumo                   │
        │  • FastAPI  ────────►  REST /API     │
        │  • React+Vite SPA   ►  dashboard.web │
        │  • Looker BI (opcional)              │
        └──────────────────────────────────────┘
```

## Filosofia de engenharia

Cinco princípios regem o código. Eles também explicam a maior parte das decisões de custo e confiabilidade descritas adiante.

1. **Workers stateless.** Cada pipeline é um container que sobe, processa, escreve e morre. Não há servidores idle na fatura.
2. **Lake antes do warehouse.** Tudo é persistido em GCS como Parquet antes de chegar ao BigQuery, então reprocessar custa zero.
3. **Schema como código.** Tabelas BigQuery são recursos Terraform versionados, e um hook de pre-commit bloqueia drift entre o schema declarado e o código.
4. **Sem LLMs em produção.** O NLP é determinístico (regex, gazetteers, dicionários), o que o mantém previsível, auditável e sem custo de inferência.
5. **Falhar alto.** Uma carga que escreve zero linhas quando se esperam linhas é um erro, não um sucesso. Essa regra foi paga em produção (veja [o incidente](#o-incidente-que-moldou-o-projeto)).

## Stack técnica

Cada escolha justificada em uma linha. Onde houver alternativa óbvia, ela aparece nomeada.

### Linguagem e tooling

| Tecnologia | Versão | Por quê |
|---|---|---|
| Python | 3.11–3.12 | Ecossistema de dados maduro, type hints, async nativo. |
| uv (workspace) | 0.4+ | Resolve em segundos, lock file único para todo o monorepo. Substituiu Poetry após a Fase 6: instala cerca de 10x mais rápido e elimina divergência de lock files entre subprojetos. |
| import-linter | 2.0+ | Contratos de arquitetura em camadas verificados em CI (`domain ◄ infra ◄ {storage,nlp,mlops} ◄ pipelines`). Sem ele, a estrutura degrada em semanas. |

### Coleta e parsing

| Tecnologia | Versão | Por quê |
|---|---|---|
| feedparser | 6.0+ | Padrão de fato para RSS, com edge cases que custariam semanas para reimplementar. |
| trafilatura | 1.8.1 | Extrai conteúdo limpo de portais de notícia, com qualidade superior a readability-lxml em PT-BR. |
| httpx | 0.27 | HTTP async com timeouts, retries e HTTP/2. Substituto direto do requests para workloads concorrentes. |
| playwright | 1.45 (opcional) | Headless browser para portais com anti-bot. Carregado sob demanda, não em todo job. |
| Apify | API v2 | Scrapers gerenciados de Facebook, Instagram, X e TikTok. Construir os nossos violaria os termos de uso e custaria mais que os R$ 50-100/mês do Apify. |

### NLP, 100% determinístico

| Tecnologia | Versão | Por quê |
|---|---|---|
| spaCy | 3.7+ | NER em português e POS tagging com modelos pré-treinados. Custo zero de inferência em CPU. |
| sentence-transformers | 3.1+ | Embeddings para clustering narrativo (Eixo 2). Roda em CPU para o nosso volume. |
| HDBSCAN | 0.8+ | Clustering hierárquico sem `k` pré-definido, essencial para tópicos emergentes. |
| BERTopic | 0.16+ | Topic modeling combinando embeddings, HDBSCAN e c-TF-IDF. |
| PyYAML | 6.0+ | Regras de pós-processamento de NER e gazetteers em YAML versionado, auditável por humanos. |
| anthropic | 0.39+ | Claude API usado apenas para explicar clusters ao usuário final (Eixo 2), nunca como passo de inferência em produção. |

O NLP de produção é determinístico de propósito: custo zero por documento, qualquer classificação se justifica lendo a regra, a mesma entrada sempre gera o mesmo output, e a latência fica abaixo de 100 ms por documento em CPU. Um LLM só entra como camada de explicação para o humano que lê o dashboard.

### Data warehouse e lake

| Tecnologia | Versão | Por quê |
|---|---|---|
| BigQuery | on-demand | Pay-per-query, particionado por data e clusterizado por município, então dashboards custam menos de R$ 0,01 por consulta. Snowflake ou Redshift exigiriam um warehouse always-on perto de R$ 200+/mês. |
| Google Cloud Storage | — | Data lake em Parquet (`raw/silver/gold`). Storage padrão custa centavos por GB. |
| DuckDB | via dbt-duckdb 1.10.1 | O mesmo SQL roda local em dev e na cloud em prod. Custo zero durante o desenvolvimento. |
| Apache Iceberg | 0.8+ (opcional) | Time-travel e schema evolution para datasets críticos. Carregado só onde é preciso. |
| PyArrow | 17.0+ | Serialização Parquet e formato colunar in-memory. |

### Transformação

| Tecnologia | Versão | Por quê |
|---|---|---|
| dbt-core | 1.11.8 | Padrão de mercado para transformação SQL: testes, lineage, docs gerados, macros. |
| dbt-bigquery | 1.11.1 | Adapter para o target de produção. |
| dbt-duckdb | 1.10.1 | Adapter para dev local: mesmo modelo SQL, target diferente. |
| sqlfluff | 3.1+ | Linter SQL. Um hook de pre-commit bloqueia sintaxe dialeto-específica (por exemplo, `INTERVAL 'N unit'` é DuckDB-only). |

### Cloud (GCP)

| Tecnologia | Por quê |
|---|---|
| Cloud Run Jobs | O container roda, processa e morre. Pay-per-execution, cerca de R$ 0,50/mês para uma execução de 2 minutos a cada 8 horas. |
| Cloud Scheduler | Cron como serviço. Grátis até 3 jobs/mês, o que cobre tudo aqui. |
| Cloud SQL (PostgreSQL 15) | Metadados operacionais (auth logs, alertas). Uma instância `db-f1-micro` a cerca de R$ 35/mês é o maior item da fatura. |
| Memorystore (Redis) | Cache de dedup e estado de circuit breaker. Substituível por Redis em VM se custo virar problema. |
| Secret Manager | Segredos versionados e controlados por IAM. O free tier cobre o uso. |
| Workload Identity Federation | GitHub Actions autentica no GCP sem chaves estáticas. Nenhuma credencial de longa duração. |
| Artifact Registry | Imagens de container por pipeline. O free tier cobre o uso. |

### Backend e frontend

| Tecnologia | Versão | Por quê |
|---|---|---|
| FastAPI | 0.110+ | Async nativo, OpenAPI grátis, validação Pydantic. Roda em Cloud Run com cold start abaixo de 2s. |
| uvicorn | 0.27+ | Servidor ASGI padrão. |
| React | 18.3 | SPA estática hospedável em GCS mais Cloud CDN por cerca de R$ 0,50/mês. |
| Vite | 5.2+ | Builds sub-segundo, HMR instantâneo. O substituto óbvio do Create React App. |
| Recharts | 2.12+ | Charts declarativos, suficientes para o caso. D3 puro não valia a complexidade extra. |
| Leaflet + react-leaflet | 1.9.4 / 4.2 | Mapas open-source sem chave de API e sem cota. |
| TanStack Query | 5.40+ | Cache de server-state no cliente, reduzindo chamadas ao BigQuery em cerca de 70%. |
| TailwindCSS | 3.4+ | Utility-first. Bundle final abaixo de 30 KB gzipped. |
| TypeScript | 5.4+ | Type safety no front, equivalente ao que o Pydantic dá no back. |

### Observabilidade e resiliência

| Tecnologia | Por quê |
|---|---|
| loguru | Logging estruturado em JSON sem boilerplate. Substituto direto do módulo logging da stdlib. |
| tenacity | Decorator de retry com exponential backoff e jitter. Uma linha de decorator no lugar de vinte de loop. |
| prometheus-client | Métricas em formato Prometheus, exportadas via Cloud Monitoring. |
| Cloud Monitoring + Alert Policies | Três políticas críticas: freshness de silver/gold, falhas de load no BQ e schema drift. |

### IaC e CI/CD

| Tecnologia | Por quê |
|---|---|
| Terraform | Toda a infra GCP é código, revisado em pull request. State remoto em GCS. |
| GitHub Actions | O free tier cobre o projeto. Change-detection por subprojeto evita rebuilds desnecessários. |
| pre-commit | Hooks locais bloqueiam SQL dialeto-específico, segredos e violações de ruff/black. |
| docker-compose | Postgres e Redis locais com a mesma configuração de produção, sem precisar de internet. |

## Decisões de custo-benefício

Cinco escolhas que diferenciam este projeto de um setup padrão de tutorial.

### 1. Cloud Run Jobs em vez de Composer/Airflow

Os pipelines rodam por dois a cinco minutos, algumas vezes ao dia. Manter um Airflow always-on (no mínimo 1 vCPU, 2 GB RAM, 24/7) custaria R$ 150-300/mês, entre 30x e 60x o custo total do projeto. Cloud Run Jobs cobram só pela execução, cerca de R$ 0,50/mês.

O trade-off aceito é não ter DAGs com dependência complexa entre tarefas. Cada pipeline aqui é um grafo linear curto, então o Cloud Scheduler basta. Se isso mudar, o diretório `services/` está pronto para receber um Cloud Workflows ou um orquestrador estilo Argo sem reescrever os pipelines.

### 2. DuckDB local, BigQuery em produção, mesmo SQL

O dbt roda com dois targets: `dev` (DuckDB) e `prod` (BigQuery). Durante o desenvolvimento, `dbt build` roda em milissegundos contra um arquivo local, com zero risco de queimar quota cobrável testando mudanças. Em produção, o BigQuery faz o trabalho pesado com particionamento e clustering.

Um hook de pre-commit bloqueia sintaxe dialeto-específica (por exemplo, `INTERVAL '1 day'` é DuckDB-only e quebra no BQ), e macros do dbt como `{{ dbt.dateadd(...) }}` abstraem aritmética temporal. O resultado é um SQL, dois alvos e nenhuma divergência silenciosa.

### 3. NLP determinístico, sem LLMs em produção

NER, sentimento e classificação de tópicos rodam via spaCy, dicionários YAML e regras Python. O custo de inferência é zero por documento, qualquer classificação se justifica lendo a regra, a mesma entrada sempre produz o mesmo output, e a latência fica abaixo de 100 ms por documento em CPU.

Há espaço para um LLM, mas como explicador. Quando um cluster narrativo aparece no dashboard, o usuário pode pedir para o sistema explicá-lo, e só então a Claude API roda. É uma chamada por interação humana, não por documento.

### 4. Data lake em GCS antes do BigQuery

Todo dado bruto chega ao GCS como Parquet antes de ser carregado no BigQuery. Reprocessar então custa zero: se a lógica de enrichment muda, os modelos dbt são reescritos sobre os mesmos arquivos Parquet. Sem essa camada, cada bug em produção forçaria re-ingestão das fontes, o que muitas vezes é impossível, já que feeds RSS não guardam histórico. O lake também preserva o dado bruto por padrão, então uma pergunta como "como esse artigo estava antes da limpeza?" sempre tem resposta.

### 5. Workload Identity Federation, sem chaves estáticas

O GitHub Actions autentica no GCP por OIDC, sem service-account keys em segredos. Isso elimina uma classe inteira de vazamento, a falha do tipo "a chave vazou no log ou no repo". Cada deploy é assinado pela identidade do workflow e pela branch, e não há chave para rotacionar porque não há chave. O trade-off é uma configuração inicial mais complexa que um JSON em GitHub Secret, mas se faz uma vez e não se mexe mais.

## O incidente que moldou o projeto

Em 18 de abril de 2026, por cerca de 24 horas, o pipeline RSS rodou normalmente no Cloud Run (`succeededCount=1` em todas as execuções), mas nenhuma linha nova chegou ao BigQuery. Os dashboards estagnaram. Nenhum alerta disparou.

A causa raiz foi uma flag faltante na configuração de carga Parquet (`ParquetOptions.enable_list_inference`), que fez o job de load no BigQuery rejeitar todos os arquivos por incompatibilidade de schema, sem erro, apenas zero linhas carregadas. O pipeline reportou sucesso porque a extração funcionou e o load "completou" com zero linhas.

O que mudou depois disso:

1. **Freshness emitter.** Um Cloud Run Job dedicado roda a cada 30 minutos, lê `__TABLES__.last_modified_time` e publica `custom.googleapis.com/mapear/freshness_minutes`. Um alerta dispara se silver ou gold passa de N minutos sem atualização.
2. **Schemas como código.** Tabelas BigQuery agora são recursos Terraform com JSON schema versionado. O pre-commit valida que mudanças no código batem com o schema declarado.
3. **Fail-loud por padrão.** Os pipelines propagam exceções, e uma carga com zero linhas em uma janela onde se esperam N linhas é tratada como erro.
4. **Testes dbt de drift parametrizados.** Cada modelo crítico tem um teste comparando o schema atual com o esperado.

A lição registrada no projeto: fail-loud é requisito de qualidade, não de conveniência. Métricas que silenciam erros são piores que não ter métricas. Post-mortem completo em [`docs/diagnostico/2026-04-18/`](docs/diagnostico/2026-04-18/).

## Camadas de dados (medallion)

```
┌─────────┐   ┌──────────┐   ┌────────┐   ┌──────────────────┐
│  raw    │ → │  silver  │ → │  gold  │ → │  marts (fct/dim) │
│  (GCS)  │   │  (stg_*) │   │ (int_*)│   │  (fct_*/dim_*)   │
└─────────┘   └──────────┘   └────────┘   └──────────────────┘
 bytes         limpo,         join          dimensional,
 imutáveis     deduplicado    cross-source  pronto para BI
```

Por que essa separação importa para custo:

- **raw** é write-once. Nunca recoletamos para reprocessar, só re-rodamos as transformações sobre o Parquet existente.
- **silver** roda apenas sobre o delta novo (via watermark), então o histórico não é reprocessado a cada execução.
- **gold** une RSS e Social via `source_type`, resolve identidades (`"prefeito de Mossoró"` para um ID canônico) e materializa os modelos pesados.
- **marts** são as tabelas que o dashboard consulta. Partição por data mais cluster por município mantêm a varredura típica abaixo de 1 MB.

Convenções de nomenclatura: `stg_<fonte>__<entidade>` (staging), `int_<dominio>__<descrição>` (intermediate), `fct_<fato>` / `dim_<dimensão>` (marts). Detalhes em [`dbt/README.md`](dbt/README.md).

## Como rodar localmente

Pré-requisitos: Python 3.11+, Docker e docker-compose, `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`) e `make`.

```bash
# 1. Subir Postgres + Redis (mesma config que produção)
make up

# 2. Instalar o workspace inteiro (um único .venv para tudo)
make install-all

# 3. Rodar o pipeline RSS em modo local (target dev = DuckDB)
make rss-pipeline

# 4. Rodar o dbt
make dbt-build

# 5. Subir o dashboard (API + frontend)
cd apps/dashboard && make dev
```

Para detalhes de cada subprojeto, veja os READMEs específicos:

- ETLs: [`pipelines/mapear-rss/README.md`](pipelines/mapear-rss/README.md), [`pipelines/mapear-social/README.md`](pipelines/mapear-social/README.md)
- Dashboard: [`apps/dashboard/README.md`](apps/dashboard/README.md)
- Modelagem: [`dbt/README.md`](dbt/README.md)
- Infra: [`infra/README.md`](infra/README.md)

## Estrutura do repositório

```
Mapear-RN/
├── libs/                    # Bibliotecas compartilhadas (pure Python, testáveis)
│   ├── mapear-domain/       # Entidades RN, resolução de pessoa, fonte de verdade dos 167 municípios
│   ├── mapear-infra/        # Config (Pydantic), logging (loguru), retry, cache, circuit breaker
│   ├── mapear-nlp/          # NER + sentimento + tópicos determinísticos em PT-BR
│   ├── mapear-storage/      # Loaders BigQuery/GCS/DuckDB, watermark, idempotência
│   └── mapear-mlops/        # MLflow para avaliação e baselines de NLP
│
├── pipelines/               # Aplicações ETL (orquestram libs + credenciais)
│   ├── mapear-rss/          # RSS: descoberta → extração (trafilatura) → enriquecimento → load
│   └── mapear-social/       # Apify (FB/IG/X/TikTok) → enriquecimento → load
│
├── services/                # Cloud Run Jobs auxiliares (alert, freshness, dbt-runner, nlp-runner, graph)
│
├── apps/
│   └── dashboard/           # FastAPI (api/) + React/Vite SPA (frontend/)
│
├── dbt/                     # Projeto dbt (mapear_rn): staging → intermediate → marts
│   ├── models/
│   ├── seeds/               # rn_cities_mayors.csv, fonte de verdade dos 167 municípios
│   └── tests/singular/      # 20+ testes de qualidade
│
├── infra/                   # Terraform (módulos por recurso GCP)
│
├── docs/                    # Post-mortems, baselines de qualidade, runbooks, dívida técnica
│
├── .github/workflows/       # CI (lint + test + import-linter) e CD (build + deploy)
│
├── Makefile                 # Targets centralizados
├── pyproject.toml           # uv workspace root
└── docker-compose.yml       # Postgres + Redis locais
```

Cada subprojeto tem seu próprio README com as decisões específicas. Comece pelos linkados em [Como rodar localmente](#como-rodar-localmente).

## Roadmap e status

| Eixo | Status | Próximo passo |
|---|---|---|
| Eixo 1 — Cobertura (RSS + Social) | ✅ Em produção | Adicionar a YouTube Data API. |
| Eixo 2 — Narrativas (clustering, embeddings, explicação via LLM) | 🟡 Em construção | Materializar `fct_narrative_cluster_*` e expor no dashboard. |
| Eixo 3 — Comunidades (grafos de co-ativação de autores) | 🟡 Em construção | `graph-runner` rodando; integrar os marts ao dashboard. |
| Qualidade de dados | ✅ 35 campos / 39 métricas | Fechar a dívida técnica de 9 gaps documentados. |
| Observabilidade | ✅ Freshness + 3 alertas | Adicionar dashboards Cloud Monitoring para SLOs. |

A dívida técnica está documentada e priorizada em [`docs/tech_debt_INDEX.md`](docs/tech_debt_INDEX.md).

## Marcos

O repositório documenta o processo, não apenas o produto final.

- **Marco 0.** Definição de escopo: RN, 167 municípios, seed CSV como fonte de verdade única.
- **Marco 1.** Primeiro pipeline RSS em produção (Cloud Run Job + Scheduler).
- **Marco 2.** O incidente de abril de 2026 e a camada de observabilidade construída em resposta.
- **Marco 3.** Sprint de qualidade de dados (35 campos derivados, 39 métricas, baselines em produção).
- **Marco 4.** Expansão para social (Apify) e generalização do warehouse para multi-fonte.
- **Marco 5.** Re-arquitetura para uv workspace com layered libs e import-linter, em 9 fases incrementais ([`ARCHITECTURE_PROPOSAL.md`](ARCHITECTURE_PROPOSAL.md)).
- **Marco 6.** Camada de consumo: API FastAPI + SPA React.

Para contribuir, leia [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Licença

MIT. Uso livre para portfólio, ensino, jornalismo e pesquisa. Atribuição apreciada.

## Contato

- Autor: Guilherme Santos Cavalcante
- Email: gui.cavalcante3o@gmail.com
- Repositório: [github.com/GscDtAnalytic/Mapear-RN](https://github.com/GscDtAnalytic/Mapear-RN)
