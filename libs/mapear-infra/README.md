# mapear-infra

[← voltar para README raiz](../../README.md)

Cross-cutting concerns: configuração, logging, retry, cache, circuit breaker, métricas. Tudo que **não é domínio** mas **toda lib precisa**.

---

## O que é

Camada que fica entre `mapear-domain` (puro) e as libs de I/O (`mapear-storage`, `mapear-nlp`). Provê os utilitários que **toda aplicação de produção precisa** mas ninguém quer reimplementar: settings tipadas, retries com backoff, circuit breaker, cliente Redis, logger estruturado, métricas Prometheus.

## Por que existe

Sem essa lib, cada pipeline teria seu próprio jeito de carregar config (alguns `.env`, outros YAML, outros direto `os.getenv`), seu próprio decorator de retry (alguns com tenacity, outros com loop manual), seu próprio formato de log. Resultado em poucos meses: caos operacional.

Centralizar aqui significa:
- **Um único formato de log** em toda a aplicação → fácil de filtrar em Cloud Logging.
- **Um único modelo de Settings** → variáveis de ambiente declaradas como código.
- **Um único circuit breaker** → comportamento uniforme contra portais flaky.

## Stack

| Tecnologia | Versão | Para quê |
|---|---|---|
| **Pydantic + pydantic-settings** | 2.9 / 2.5 | Config tipada, validada, com defaults explícitos. |
| **loguru** | 0.7 | Logging estruturado (JSON em prod, colorido em dev). |
| **tenacity** | 9.0 | Retry com exponential backoff + jitter, decorator-based. |
| **httpx** | 0.27 | Cliente HTTP async com timeouts e instrumentação. |
| **redis** | 5.1 | Cliente Redis (cache, dedup, circuit breaker state). |
| **SQLAlchemy** | 2.0 | ORM para metadata em Postgres. |
| **psycopg2-binary** | 2.9 | Driver Postgres. |
| **prometheus-client** | — | Métricas em formato Prometheus. |
| **opentelemetry** | — | Tracing distribuído (opcional). |

## API pública (essencial)

```python
from mapear_infra.config import Settings           # Pydantic settings base
from mapear_infra.logging import configure_logging
from mapear_infra.retry import retryable
from mapear_infra.circuit_breaker import CircuitBreaker
from mapear_infra.cache import RedisCache

@retryable(max_attempts=5, wait_min=1, wait_max=30)
def fetch_feed(url: str) -> bytes:
    ...
```

`Settings` é estendida por cada subprojeto (RSS adiciona `ScraperConfig`, social adiciona `ApifyConfig`).

## Decisões locais

1. **Settings via Pydantic, não dotenv puro.** Validação no startup falha cedo: se faltar uma var de ambiente crítica, o container morre antes de chamar BigQuery.
2. **Circuit breaker com estado em Redis.** Portais com problema persistente são "abertos" por N minutos sem que cada execução do Cloud Run Job descubra o problema do zero.
3. **Sem cliente cloud específico aqui.** Google Cloud SDK fica em `mapear-storage` — `mapear-infra` continua agnóstica de provedor. Trocar de cloud não exige tocar esta lib.

## Como testar

```bash
uv run pytest libs/mapear-infra/tests/
```

## Estrutura

```
mapear-infra/
├── src/mapear_infra/
│   ├── config.py              # Settings base (Pydantic)
│   ├── logging.py             # loguru bootstrap
│   ├── retry.py               # tenacity wrappers
│   ├── circuit_breaker.py     # Estado em Redis
│   ├── cache.py               # RedisCache, TTLCache
│   ├── metrics.py             # Prometheus counters/histograms
│   └── db.py                  # SQLAlchemy engine factory (Postgres)
└── tests/
```
