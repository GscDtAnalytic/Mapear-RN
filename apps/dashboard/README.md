# apps/dashboard

[← voltar para README raiz](../../README.md)

Camada de consumo do Mapear-RN: **API FastAPI** sobre BigQuery + **SPA React + Vite** com mapas e charts.

---

## O que é

A "ponta final" do projeto — onde a pesquisa, o jornalismo de dados ou o monitoramento cívico acontece. Dois componentes acoplados:

- **`api/`** — FastAPI que serve endpoints REST sobre o BigQuery (marts dbt). Cache em memória (`cachetools`) para queries frequentes.
- **`frontend/`** — SPA em React + Vite + TypeScript com mapas (Leaflet) dos 167 municípios, charts (Recharts), e componentes Tremor para listas e KPIs.

## Por que existe (e por que não Looker Studio)

A primeira tentativa foi **Looker Studio** apontando direto para o BigQuery — barato, rápido de montar. Limitação que matou a opção:

- **Mapas customizados de 167 municípios do RN** não são suportados nativamente; o "geo chart" do Looker assume granularidade estado/país.
- **Interatividade entre componentes** é limitada — não dá para clicar num município e filtrar narrativas, autores, sentimento, tudo de uma vez.
- **Custo por consulta** cresce com cada filtro; sem cache no cliente, o BQ é golpeado a cada interação.

A SPA React resolve os três: mapa próprio em Leaflet, estado global compartilhado via TanStack Query, cache client-side reduzindo chamadas ao BQ em ~70%.

## Stack

### Backend (`api/`)

| Tecnologia | Versão | Para quê |
|---|---|---|
| **FastAPI** | 0.110+ | Endpoints REST, OpenAPI grátis, async. |
| **uvicorn[standard]** | 0.27+ | ASGI server, websockets, http2. |
| **google-cloud-bigquery** | 3.17+ | Queries sobre as tabelas mart. |
| **cachetools** | 5.3 | TTLCache em memória para queries frequentes. |
| **db-dtypes** | 1.2 | Tipos pandas compatíveis com BQ. |
| **anthropic** | 0.39+ | Claude como **explicador** opcional de clusters narrativos. |
| **sentence-transformers** | 3.1 | Embeddings on-the-fly para busca semântica. |

### Frontend (`frontend/`)

| Tecnologia | Versão | Para quê |
|---|---|---|
| **React** | 18.3 | SPA. |
| **Vite** | 5.2 | Bundler + dev server. |
| **TypeScript** | 5.4 | Type safety. |
| **TanStack React Query** | 5.40 | Cache de servidor no cliente. |
| **Recharts** | 2.12 | Charts (line, bar, area). |
| **Leaflet** + **react-leaflet** | 1.9.4 / 4.2 | Mapas dos 167 municípios. |
| **Tremor** | 3.18 | Componentes de dashboard (KPI cards, tables). |
| **TailwindCSS** | 3.4 | Utility-first CSS. |
| **react-router-dom** | 6.23 | Roteamento client-side. |
| **axios** | 1.7 | HTTP client (poderia ser fetch; preferência por interceptors). |

## Como rodar

```bash
# 1. Backend (porta 8000)
cd apps/dashboard/api
uv run uvicorn main:app --reload --port 8000

# 2. Frontend (porta 5173)
cd apps/dashboard/frontend
npm install
npm run dev

# Build de produção do frontend (estático em dist/)
npm run build
```

Variáveis de ambiente do backend (em `.env`):
- `GOOGLE_APPLICATION_CREDENTIALS` ou Workload Identity em produção
- `BQ_PROJECT_ID`, `BQ_DATASET_MARTS`
- `ANTHROPIC_API_KEY` (opcional, para explicação de clusters)

## Decisões locais

1. **API e frontend acoplados no mesmo subprojeto, mas independentes.** Frontend é estático puro (HTML/CSS/JS) hospedado em GCS + Cloud CDN (~R$ 0,50/mês). Backend é Cloud Run service com cold-start < 2s.
2. **Cache no backend é TTLCache, não Redis.** O dashboard tem dezenas de usuários, não milhares. TTLCache em memória é suficiente e elimina uma dependência operacional. *Se virar problema, troca para Redis em uma linha.*
3. **TanStack Query no cliente é o cache principal.** Componentes reaproveitam dados entre rotas; cada chamada ao BQ é uma raridade.
4. **Claude API só sob interação humana.** Endpoint `/explain/cluster/{id}` chama o LLM apenas quando o usuário clica "explicar". Custo controlado, fila não-bloqueante.
5. **Frontend não conhece o BigQuery.** Toda lógica de negócio fica na API — frontend é dumb (UI puro). Permite trocar BQ por outro warehouse sem tocar o frontend.

## Estrutura

```
apps/dashboard/
├── api/
│   ├── main.py                # FastAPI app
│   ├── bq.py                  # BigQuery client + helpers
│   ├── routers/               # /content, /entities, /narratives, /map
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── main.tsx
│   │   ├── pages/             # Telas do dashboard
│   │   ├── components/        # Mapa, charts, KPIs
│   │   ├── api/               # Cliente HTTP tipado
│   │   ├── context/           # Estado global (filtros, seleção)
│   │   └── types/             # TypeScript types compartilhados
│   ├── package.json
│   └── vite.config.ts
├── Dockerfile                 # Container da API (frontend é estático separado)
├── Makefile                   # `make dev`, `make build`, `make deploy`
└── pyproject.toml             # Deps Python do backend
```

## Como testar

```bash
# Backend
cd apps/dashboard/api
uv run pytest

# Frontend
cd apps/dashboard/frontend
npm run test     # quando configurado
npm run build    # type-check estrito (tsc) + bundle
```

## Deploy

- **API:** container publicado no Artifact Registry, deployed como Cloud Run service via Terraform.
- **Frontend:** `npm run build` → upload do `dist/` para GCS, servido via Cloud CDN.

Custo total da camada de consumo em produção: **~R$ 1/mês** (CDN + Cloud Run cold-warm).
