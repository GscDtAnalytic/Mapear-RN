# mapear-social

[← voltar para README raiz](../../README.md)

Pipeline ETL para conteúdo de Facebook, Instagram, X e TikTok via Apify. Roda em cadência variável por rede.

---

## O que é

Equivalente social do `mapear-rss`. Em vez de feeds, consome scrapers gerenciados do **Apify**:

1. **Dispara** runs de Apify actors (um por rede), passando handles de perfis monitorados (prefeitos, vereadores, perfis oficiais).
2. **Aguarda** o resultado, baixa o dataset.
3. **Normaliza** posts de cada rede em um modelo comum (`SocialPost`).
4. **Enriquece** com `mapear-nlp` (mesmo enrichment do RSS, com adaptações para textos curtos).
5. **Carrega** em GCS + BigQuery (`social_raw`, `social_silver`).

A cadência varia por rede e é controlada por Cloud Scheduler — redes mais ativas (X, Instagram) rodam mais frequentemente.

## Por que existe

A cobertura midiática profissional (RSS) é só metade da história. **Onde a opinião se forma** hoje é em redes sociais. Para um observatório sócio-político ser relevante, precisa capturar:

- Posts de prefeitos e vereadores (canal direto de comunicação política).
- Comentários e engajamento (proxy de reação pública).
- Cross-posting entre redes (mesma narrativa, alcances diferentes).

## Por que Apify e não scrapers próprios

| Critério | Scraper próprio | Apify |
|---|---|---|
| **Custo de desenvolvimento** | Semanas por rede, manutenção constante | Zero |
| **Custo de execução** | Infra própria + proxies (R$ 100+/mês) | R$ 50-100/mês total |
| **Compliance com ToS** | Ambíguo, frequentemente violado | Gerenciado pelo provedor |
| **Resistência a mudanças de UI** | Quebra a cada update | Apify mantém |

A escolha foi pragmática: pagamos R$ 50-100/mês para não construir nem manter scrapers de 4 redes. Os actor IDs estão fixados em [`infra/prod.tfvars.example`](../../infra/prod.tfvars.example).

## Stack local

| Tecnologia | Versão | Para quê |
|---|---|---|
| **httpx** | 0.27 | Cliente HTTP async para a API do Apify. |
| **mapear-domain / -infra / -nlp / -storage** | — | Toda a base. |
| **google-cloud-storage / -bigquery / -language** | — | Carga e (opcional) NLP API do GCP. |

## Como rodar

```bash
# Local (target dev = DuckDB, usa fixtures por padrão)
ENVIRONMENT=local uv run python -m mapear_social

# Apontando para Apify real (precisa APIFY_TOKEN configurado)
ENVIRONMENT=local APIFY_TOKEN=... uv run python -m mapear_social --networks facebook,instagram
```

## Decisões locais

1. **Modelo comum `SocialPost`, normalização na entrada.** Cada rede tem seu shape; convertemos imediatamente para um schema unificado. A partir daí, o pipeline trata tudo igual.
2. **Source-of-truth de perfis monitorados.** Lista de handles fica em config versionada — adicionar um vereador novo é um PR, não uma query SQL no banco.
3. **Idempotência por `(network, post_id)`.** Apify pode retornar duplicatas entre runs; dedup é feito no silver via dbt.
4. **NLP para textos curtos.** Posts são tipicamente < 500 caracteres; alguns componentes do `mapear-nlp` têm modo "short-text" que pula etapas (ex.: clustering narrativo só roda em batches agregados, não por post).

## Estrutura

```
mapear-social/
├── src/mapear_social/
│   ├── __main__.py
│   ├── apify/                 # Cliente Apify (start, poll, fetch dataset)
│   ├── normalization/         # Cada rede → SocialPost
│   ├── enrichment/            # Adaptação do NLP para textos curtos
│   ├── loaders/
│   ├── config.py
│   └── pipeline.py
├── tests/
└── pyproject.toml
```

## Como testar

```bash
uv run pytest pipelines/mapear-social/tests/
```

Testes usam fixtures de respostas do Apify (sem chamar a API real em CI).

## Schedule em produção

Cadência variável por rede, configurada em [`infra/modules/cloud_scheduler/`](../../infra/modules/cloud_scheduler/).
