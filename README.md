# Mapear-RN

> Plataforma ETL de monitoramento sócio-político do Rio Grande do Norte — **167 municípios, multi-fonte, custo < R$ 5/mês em produção**.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)]() [![uv workspace](https://img.shields.io/badge/uv-workspace-purple)]() [![dbt 1.11](https://img.shields.io/badge/dbt-1.11.8-orange)]() [![Terraform](https://img.shields.io/badge/IaC-Terraform-7B42BC)]() [![GCP](https://img.shields.io/badge/cloud-GCP-4285F4)]() [![License](https://img.shields.io/badge/license-MIT-green)]()

---

## O que é, em uma linha

Um pipeline de dados de **produção** que coleta tudo o que portais regionais e redes sociais publicam sobre **prefeitos, vereadores e narrativas políticas dos 167 municípios do RN**, enriquece com NLP em português, modela em um data warehouse e disponibiliza via API e dashboard.

> **Para quem é este README:** tech leads e recrutadores que querem entender, em 3-5 minutos, **o quê foi construído, com quais tecnologias e por quê** — com foco honesto em custo-benefício e nas decisões que importam.
>
> Detalhes técnicos profundos de cada componente moram nos READMEs dos subprojetos (links no final).

---

## Sumário

- [TL;DR](#tldr)
- [Arquitetura em 30 segundos](#arquitetura-em-30-segundos)
- [Stack técnica](#stack-técnica)
- [Decisões de custo-benefício](#decisões-de-custo-benefício)
- [O incidente que moldou o projeto](#o-incidente-que-moldou-o-projeto)
- [Camadas de dados (medallion)](#camadas-de-dados-medallion)
- [Como rodar localmente](#como-rodar-localmente)
- [Estrutura do repositório](#estrutura-do-repositório)
- [Roadmap e status](#roadmap-e-status)
- [Sobre o desenvolvimento](#sobre-o-desenvolvimento)

---

## TL;DR

| Pergunta | Resposta curta |
|---|---|
| **Qual o problema?** | A cobertura sócio-política dos 167 municípios do RN é fragmentada entre portais regionais e redes sociais. Não há série temporal, não há agregação. |
| **Qual a solução?** | Coletar (RSS + Apify), padronizar, enriquecer com NLP determinístico em português e modelar com dbt em um warehouse cloud. |
| **Quais fontes?** | Portais de notícia (RSS) + Facebook, Instagram, X, TikTok (Apify). YouTube planejado. |
| **Onde roda?** | Google Cloud Platform — Cloud Run Jobs disparados por Cloud Scheduler, dados em GCS + BigQuery. |
| **Quanto custa?** | **< R$ 5/mês** em produção. Comparação: a mesma arquitetura com Composer/Airflow always-on passaria de R$ 150/mês. |
| **Quem consome?** | Dashboard React + API FastAPI sobre BigQuery; modelos dbt prontos para BI. |

---

## Arquitetura em 30 segundos

```
                ┌───────────────────────────────────────────────────────────────┐
                │  FONTES                                                       │
                │  RSS feeds  •  Facebook / Instagram / X / TikTok (via Apify)  │
                └────────────────────────────┬──────────────────────────────────┘
                                             │
                                             ▼
              ┌──────────────────────────────────────────────────────────┐
              │  INGESTÃO — Cloud Run Jobs (cron via Cloud Scheduler)    │
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

**Princípios:**

1. **Stateless workers**: cada pipeline é um container que roda, processa, escreve e morre. Sem servidores idle.
2. **Lake antes do warehouse**: tudo é persistido em GCS como Parquet antes de chegar ao BigQuery — reprocessamento custa zero.
3. **Schema como código**: tabelas BigQuery são recursos Terraform versionados; pre-commit bloqueia drift.
4. **Sem LLMs em produção**: NLP determinístico (regex + gazetteer + dicionários) — previsível, auditável, custo zero de inferência.

---

## Stack técnica

Cada escolha justificada em uma linha. Onde houver alternativa óbvia, ela aparece em itálico.

### Linguagem e gerenciamento

| Tecnologia | Versão | Por que essa escolha |
|---|---|---|
| **Python** | 3.11–3.12 | Ecossistema de dados maduro, type hints, async nativo. |
| **uv** (workspace) | 0.4+ | Resolve em segundos, lock file único para todo o monorepo. *Substituiu Poetry após a Fase 6 — instalação 10× mais rápida e elimina divergência de lock files entre subprojetos.* |
| **import-linter** | 2.0+ | Contratos de arquitetura em camadas verificáveis em CI (`domain ◄ infra ◄ {storage,nlp,mlops} ◄ pipelines`). Sem ele, a estrutura degrada em semanas. |

### Coleta e parsing

| Tecnologia | Versão | Por que essa escolha |
|---|---|---|
| **feedparser** | 6.0+ | Padrão de fato para RSS — handles edge cases que reescrever do zero custaria semanas. |
| **trafilatura** | 1.8.1 | Extrai conteúdo limpo de portais de notícia com qualidade superior a `readability-lxml` em PT-BR. |
| **httpx** | 0.27 | HTTP async com timeouts, retries e suporte a HTTP/2 — substituto direto do `requests` para workloads concorrentes. |
| **playwright** | 1.45 (opcional) | Headless browser para portais com anti-bot. Carregado sob demanda; não roda em todo job. |
| **Apify** | API v2 | Scrapers gerenciados de Facebook/Instagram/X/TikTok. *Construir scrapers próprios desses sites violaria ToS e custaria mais que os R$ 50-100/mês do Apify.* |

### NLP — 100% determinístico

| Tecnologia | Versão | Por que essa escolha |
|---|---|---|
| **spaCy** | 3.7+ | NER em português + POS tagging com modelos pré-treinados. Custo zero de inferência em CPU. |
| **sentence-transformers** | 3.1+ | Embeddings para clustering narrativo (Eixo 2). Roda em CPU para nosso volume. |
| **HDBSCAN** | 0.8+ | Clustering hierárquico que não exige `k` pré-definido — essencial para tópicos emergentes. |
| **BERTopic** | 0.16+ | Topic modeling combinando embeddings + HDBSCAN + c-TF-IDF. |
| **PyYAML** | 6.0+ | Regras de pós-processamento de NER e gazetteers em YAML versionado — auditável por humanos. |
| **anthropic** | 0.39+ | Claude API usado **apenas como explicador** de clusters (Eixo 2), nunca como inferidor de produção. |

> **Por que sem LLMs em produção?** Custo previsível (zero por inferência), auditabilidade (cada decisão tem um motivo no código), latência baixa, e baseline reprodutível. LLM entra só como camada de explicação para usuário final.

### Data warehouse e lake

| Tecnologia | Versão | Por que essa escolha |
|---|---|---|
| **BigQuery** | on-demand | Pay-per-query; partição por data + cluster por município → dashboards custam < R$ 0,01/consulta. *Alternativa (Snowflake/Redshift) exigiria warehouse always-on ≈ R$ 200+/mês.* |
| **Google Cloud Storage** | — | Data lake em Parquet (`raw/silver/gold`). Storage padrão custa centavos por GB. |
| **DuckDB** | via dbt-duckdb 1.10.1 | Mesmo SQL roda local (dev) e na cloud (prod). Zero custo durante desenvolvimento. |
| **Apache Iceberg** | 0.8+ (opcional) | Time-travel e schema evolution para datasets críticos. Carregado em pipelines que precisam. |
| **PyArrow** | 17.0+ | Serialização Parquet, formato in-memory colunar. |

### Transformação

| Tecnologia | Versão | Por que essa escolha |
|---|---|---|
| **dbt-core** | 1.11.8 | Padrão de mercado para transformações SQL — testes, lineage, docs gerados, macros. |
| **dbt-bigquery** | 1.11.1 | Adapter para o target de produção. |
| **dbt-duckdb** | 1.10.1 | Adapter para dev local — mesmo modelo SQL, target diferente. |
| **sqlfluff** | 3.1+ | Linter SQL; pre-commit hook bloqueia sintaxe dialeto-específica (ex.: `INTERVAL 'N unit'` é DuckDB-only). |

### Cloud (GCP)

| Tecnologia | Por que essa escolha |
|---|---|
| **Cloud Run Jobs** | Container roda, processa, morre. Pay-per-execution. ≈ R$ 0,50/mês para ~2 min a cada 8h. |
| **Cloud Scheduler** | Cron como serviço. R$ 0 até 3 jobs/mês (free tier cobre tudo). |
| **Cloud SQL (PostgreSQL 15)** | Metadados operacionais (auth logs, alertas). Instância `db-f1-micro` ≈ R$ 35/mês — *o maior item da fatura.* |
| **Memorystore (Redis)** | Dedup cache e estado de circuit breaker. *Substituível por Redis em VM se custo virar problema.* |
| **Secret Manager** | Segredos versionados, IAM-controlled. Free tier cobre o uso. |
| **Workload Identity Federation** | GitHub Actions ↔ GCP sem chaves estáticas em segredo. Zero credenciais de longa duração. |
| **Artifact Registry** | Container images por pipeline. Free tier cobre o uso. |

### Backend & frontend

| Tecnologia | Versão | Por que essa escolha |
|---|---|---|
| **FastAPI** | 0.110+ | Async nativo, OpenAPI grátis, validação Pydantic. Roda em Cloud Run com cold start < 2s. |
| **uvicorn** | 0.27+ | ASGI server padrão. |
| **React** | 18.3 | SPA estática hospedável em GCS + Cloud CDN ≈ R$ 0,50/mês. |
| **Vite** | 5.2+ | Build sub-segundo, HMR instantâneo. *Substituto óbvio do Create React App.* |
| **Recharts** | 2.12+ | Charts declarativos, suficientes para o caso. *Evitamos D3 puro porque o ganho não justifica a complexidade.* |
| **Leaflet + react-leaflet** | 1.9.4 / 4.2 | Mapas open-source; sem chave de API, sem cota. |
| **TanStack Query** | 5.40+ | Cache de servidor no cliente — reduz chamadas ao BigQuery em ~70%. |
| **TailwindCSS** | 3.4+ | Utility-first. Bundle final < 30 KB gzipped. |
| **TypeScript** | 5.4+ | Type safety no front equivale ao que Pydantic dá no back. |

### Observabilidade & resiliência

| Tecnologia | Por que essa escolha |
|---|---|
| **loguru** | Logging estruturado JSON sem boilerplate. Substituto direto de `logging` stdlib. |
| **tenacity** | Retry decorator com exponential backoff + jitter — uma linha de decorator vs 20 linhas de loop. |
| **prometheus-client** | Métricas em formato Prometheus, exportadas via Cloud Monitoring. |
| **Cloud Monitoring + Alert Policies** | 3 políticas críticas: freshness de silver/gold, falhas de load BQ, schema drift. |

### IaC & CI/CD

| Tecnologia | Por que essa escolha |
|---|---|
| **Terraform** | Toda infra GCP é código revisado em PR. State remoto em GCS. |
| **GitHub Actions** | Free tier cobre o projeto. Change-detection por subprojeto evita rebuilds desnecessários. |
| **pre-commit** | Hooks locais bloqueiam SQL dialeto-específico, segredos, e ruff/black fora do padrão. |
| **docker-compose** | Postgres + Redis locais — mesma configuração que produção, sem internet. |

---

## Decisões de custo-benefício

Cinco escolhas que diferenciam este projeto de um setup "padrão de tutorial":

### 1. Cloud Run Jobs em vez de Composer/Airflow

**Decisão:** orquestração com Cloud Scheduler disparando containers stateless.

**Por quê:** os pipelines rodam por 2-5 minutos, algumas vezes ao dia. Manter um Airflow always-on (≥ 1 vCPU, 2 GB RAM, 24/7) custaria **R$ 150-300/mês** — entre 30× e 60× o custo total do projeto. Cloud Run Jobs custam apenas pela execução: **~R$ 0,50/mês**.

**Trade-off aceito:** sem DAGs com dependência complexa entre tarefas. Para nossos pipelines (cada um é um grafo linear curto), Cloud Scheduler basta. Se isso mudar, há um diretório `services/` pronto para receber um Cloud Workflows ou um Argo Workflows-like sem refatorar pipelines.

### 2. DuckDB local + BigQuery em produção (mesmo SQL)

**Decisão:** dbt com dois targets — `dev` (DuckDB) e `prod` (BigQuery).

**Por quê:** durante desenvolvimento, rodamos `dbt build` em milissegundos contra um arquivo local. Zero risco de queimar quota cobrável testando mudanças. Em produção, BigQuery faz o trabalho pesado com partição + cluster.

**Como funciona:** pre-commit hook bloqueia sintaxe dialeto-específica (ex.: `INTERVAL '1 day'` é DuckDB-only e quebra no BQ). Macros do dbt (`{{ dbt.dateadd(...) }}`) abstraem aritmética temporal. Resultado: **um SQL, dois alvos, zero divergência silenciosa**.

### 3. NLP determinístico (sem LLMs em produção)

**Decisão:** NER, sentimento e classificação de tópicos rodam via spaCy + dicionários YAML + regras Python.

**Por quê:** custo de inferência **zero por documento**. Auditabilidade total: qualquer classificação pode ser justificada lendo a regra. Reprodutibilidade: a mesma entrada gera o mesmo output, sempre. Latência: < 100 ms por documento em CPU.

**LLM tem espaço aqui?** Sim, como **explicador**: ao mostrar um cluster narrativo no dashboard, o usuário pode pedir "explique esse cluster" e aí sim Claude API entra — uma chamada por interação humana, não por documento.

### 4. GCS Data Lake antes do BigQuery

**Decisão:** todo dado bruto vai primeiro como Parquet para GCS, e só depois é carregado no BigQuery.

**Por quê:** reprocessar custa zero. Se a lógica de enrichment muda, reescrevemos os modelos dbt apontando para os mesmos Parquets. Sem essa camada, cada bug encontrado em produção forçaria re-ingestão de fontes — frequentemente impossível (RSS feeds não mantêm histórico).

**Bônus:** auditoria. O dado bruto fica preservado por padrão; qualquer pergunta tipo "como esse artigo estava antes da limpeza?" tem resposta.

### 5. Workload Identity Federation (zero chaves estáticas)

**Decisão:** GitHub Actions autentica no GCP via OIDC — sem service account keys em segredos.

**Por quê:** elimina a classe inteira de vazamento "key vazou no log/repo". Cada deploy é assinado pela identidade do workflow + branch. Rotação não existe porque não há chave. *Trade-off:* configuração inicial mais complexa que um JSON em GitHub Secret — uma vez, e nunca mais se mexe.

---

## O incidente que moldou o projeto

Em **18 de abril de 2026**, durante ~24 horas, o pipeline RSS executou normalmente em Cloud Run (`succeededCount=1` em todas as execuções) — mas **nenhuma linha nova chegou ao BigQuery**. Os dashboards estagnaram. Nenhum alerta disparou.

**Causa raiz:** uma flag faltante na configuração de carga Parquet (`ParquetOptions.enable_list_inference`) fez o job de load no BigQuery rejeitar todos os arquivos por incompatibilidade de schema — sem erro, apenas zero linhas carregadas. O pipeline reportou sucesso porque o passo de extração funcionou e o load "completou" (com zero linhas).

**O que mudou depois disso:**

1. **Freshness emitter** — um Cloud Run Job dedicado roda a cada 30 minutos, lê `__TABLES__.last_modified_time` e publica `custom.googleapis.com/mapear/freshness_minutes`. Alerta dispara se silver/gold passa de N minutos sem atualização.
2. **Schemas como código** — tabelas BigQuery agora são recursos Terraform com JSON schema versionado. Pre-commit valida que mudanças no código batem com o schema declarado.
3. **Fail-loud por padrão** — pipelines propagam exceções; uma carga com zero linhas em uma janela onde se espera N linhas é tratada como erro, não como sucesso.
4. **Testes dbt parametrizados de drift** — cada modelo crítico tem um teste que compara o schema atual com o esperado.

**Lição registrada no projeto:** *fail-loud é requisito de qualidade, não de conveniência.* Métricas que silenciam erros são piores do que não ter métricas.

Detalhes: [`docs/diagnostico/2026-04-18/`](docs/diagnostico/2026-04-18/) (post-mortem completo).

---

## Camadas de dados (medallion)

```
┌─────────┐   ┌──────────┐   ┌────────┐   ┌──────────────────┐
│  raw    │ → │  silver  │ → │  gold  │ → │  marts (fct/dim) │
│  (GCS)  │   │  (stg_*) │   │ (int_*)│   │  (fct_*/dim_*)   │
└─────────┘   └──────────┘   └────────┘   └──────────────────┘
   bytes        limpo,         join         dimensional,
   imutáveis    deduplicado    cross-source pronto para BI
```

**Por que essa separação importa para custo:**

- **raw** é write-once. Nunca reprocessamos coletando de novo — só re-rodamos as transformações sobre o Parquet existente.
- **silver** roda apenas sobre o delta novo (via watermark) — não reprocessamos histórico em cada execução.
- **gold** une RSS + Social via `source_type`, resolve identidades (`"prefeito de Mossoró"` → ID canônico) e materializa os modelos pesados.
- **marts** são as tabelas que o dashboard consulta. Partição por data + cluster por município → varredura típica < 1 MB.

Convenções de nomenclatura: `stg_<fonte>__<entidade>` (staging), `int_<dominio>__<descrição>` (intermediate), `fct_<fato>` / `dim_<dimensão>` (marts). Detalhes em [`dbt/README.md`](dbt/README.md).

---

## Como rodar localmente

**Pré-requisitos:** Python 3.11+, Docker + docker-compose, `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`), `make`.

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

Para detalhes de cada subprojeto, ver os READMEs específicos:

- ETLs: [`pipelines/mapear-rss/README.md`](pipelines/mapear-rss/README.md), [`pipelines/mapear-social/README.md`](pipelines/mapear-social/README.md)
- Dashboard: [`apps/dashboard/README.md`](apps/dashboard/README.md)
- Modelagem: [`dbt/README.md`](dbt/README.md)
- Infra: [`infra/README.md`](infra/README.md)

---

## Estrutura do repositório

```
Mapear-RN/
├── libs/                    # Bibliotecas compartilhadas (pure Python, testáveis)
│   ├── mapear-domain/       # Entidades RN, resolução de pessoa, source-of-truth dos 167 municípios
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
├── dbt/                     # Projeto dbt (mapear_rn) — staging → intermediate → marts
│   ├── models/
│   ├── seeds/               # rn_cities_mayors.csv — fonte de verdade dos 167 municípios
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

Cada subprojeto tem seu próprio README com decisões específicas. Comece pelos linkados em [Como rodar localmente](#como-rodar-localmente).

---

## Roadmap e status

| Eixo | Status | Próximo passo |
|---|---|---|
| **Eixo 1 — Cobertura** (RSS + Social) | ✅ Em produção | Adicionar YouTube Data API. |
| **Eixo 2 — Narrativas** (clustering, embeddings, explicação via LLM) | 🟡 Em construção | Materializar `fct_narrative_cluster_*` e expor no dashboard. |
| **Eixo 3 — Comunidades** (grafos de co-ativação de autores) | 🟡 Em construção | `graph-runner` rodando; integrar marts ao dashboard. |
| **Qualidade de dados** | ✅ 35 campos / 39 métricas | Fechar dívida técnica de 9 gaps documentados. |
| **Observabilidade** | ✅ Freshness + 3 alertas | Adicionar dashboards Cloud Monitoring para SLOs. |

Dívida técnica documentada e priorizada em [`docs/tech_debt_INDEX.md`](docs/tech_debt_INDEX.md).

---

## Sobre o desenvolvimento

Este projeto foi construído em **pair-programming com Claude Code** (declarado abertamente, sem mistério). As decisões arquiteturais e os trade-offs são humanos e defendidos — o que está documentado aqui é o resultado de revisões, refatorações e, em pelo menos um caso ([o incidente de abril](#o-incidente-que-moldou-o-projeto)), de aprender errando em produção.

O repositório documenta o **processo**, não apenas o produto final. Os principais marcos:

- **Marco 0** — Definição de escopo: RN, 167 municípios, seed CSV como fonte de verdade única.
- **Marco 1** — Primeiro pipeline RSS em produção (Cloud Run Job + Scheduler).
- **Marco 2** — Incidente de abril/2026 e construção da camada de observabilidade.
- **Marco 3** — Sprint de qualidade de dados (35 campos derivados, 39 métricas, baselines em produção).
- **Marco 4** — Expansão para social (Apify) e generalização do warehouse para multi-fonte.
- **Marco 5** — Re-arquitetura para uv workspace + layered libs + import-linter, em 9 fases incrementais ([`ARCHITECTURE_PROPOSAL.md`](ARCHITECTURE_PROPOSAL.md)).
- **Marco 6** — Camada de consumo: API FastAPI + SPA React.

---

## Licença

MIT — uso livre para portfólio, ensino, jornalismo e pesquisa. Atribuição apreciada.

## Contato

- **Autor:** GUILHERME SANTOS CAVALCANTE
- **Email:** gui.cavalcante3o@gmail.com
- **Repositório:** [github.com/GscDtAnalytic/Mapear-RN](https://github.com/GscDtAnalytic/Mapear-RN)
