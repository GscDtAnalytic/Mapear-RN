# infra — Terraform

[← voltar para README raiz](../README.md)

Infraestrutura como código de toda a stack GCP. Cada recurso versionado, revisado em PR, com state remoto em GCS.

---

## O que é

Todo o ambiente cloud do Mapear-RN é descrito aqui. Não há recurso clicado no console — se está em produção, está em Terraform.

Cobre: Cloud Run Jobs e Services, Cloud Scheduler, BigQuery (datasets, tabelas, schemas), GCS buckets, Cloud SQL, Memorystore (Redis), Artifact Registry, Secret Manager, Workload Identity Federation, políticas de alerta e dashboards de monitoramento.

## Por que assim

1. **Reprodutibilidade.** Subir o ambiente do zero é `terraform apply`. Útil para staging, disaster recovery, e demos.
2. **Revisão de mudanças.** Cada `terraform plan` em PR mostra exatamente o que muda. Acabou a discussão "quem mexeu nesse alerta?".
3. **Schemas BQ como código.** [Lição do incidente de abril/2026](../README.md#o-incidente-que-moldou-o-projeto): tabelas críticas têm JSON schema versionado em [`modules/bigquery/schemas/`](modules/bigquery/schemas/). Drift entre código Python e schema declarado é detectado em pre-commit.
4. **Workload Identity Federation.** GitHub Actions deploya sem chaves estáticas. Zero credenciais de longa duração no repo.

## Custos em produção (estimativa mensal)

| Recurso | Configuração | Custo |
|---|---|---|
| **Cloud Run Jobs** (todos os pipelines) | ~2-5 min de execução, algumas vezes/dia | ~R$ 0,50 |
| **Cloud Run Services** (API dashboard, freshness emitter, alert runner) | Cold-warm, low traffic | ~R$ 1,00 |
| **BigQuery** | On-demand, queries particionadas + clusterizadas | ~R$ 1,00 |
| **GCS** | Storage padrão, ~10 GB | ~R$ 0,50 |
| **Cloud SQL (db-f1-micro)** | PostgreSQL 15 para metadata | ~R$ 35,00 ⚠️ |
| **Memorystore (Redis Basic)** | 1 GB | ~R$ 30,00 ⚠️ |
| **Cloud Scheduler** | < 3 jobs | R$ 0 (free tier) |
| **Artifact Registry, Secret Manager, Monitoring** | uso baixo | R$ 0 (free tier) |
| **Cloud CDN + GCS estático (frontend)** | ~1 GB/mês | ~R$ 0,50 |
| **Total** | | **~R$ 68/mês com Postgres+Redis; ~R$ 3/mês sem eles** |

> **Atenção:** o custo total **< R$ 5/mês** citado no README raiz refere-se ao núcleo da arquitetura (compute + storage). Postgres e Redis adicionam ~R$ 65/mês — são úteis mas substituíveis. Para uma versão ainda mais barata, ambos podem rodar em uma VM `e2-micro` (≈ R$ 25/mês total) ou serem dispensados completamente para um caso de uso menor (watermark em arquivo, sem circuit breaker compartilhado).

## Comparação com alternativas

| Stack equivalente | Custo estimado/mês |
|---|---|
| **Mapear-RN atual (Cloud Run + BQ on-demand)** | **R$ 3-70** |
| Cloud Composer (Airflow gerenciado) + Cloud SQL | R$ 600+ |
| GKE Autopilot + Airflow + Postgres | R$ 200+ |
| EC2/Compute Engine always-on + Airflow self-managed | R$ 150-300 |

A escolha por Cloud Run Jobs + Scheduler em vez de Airflow gerenciado é a decisão de maior impacto financeiro do projeto.

## Stack

| Tecnologia | Para quê |
|---|---|
| **Terraform** ≥ 1.6 | IaC. State remoto em bucket GCS. |
| **Google Provider** (latest) | Recursos GCP. |
| **Workload Identity Federation** | Auth GitHub → GCP sem chaves. |

## Como aplicar (com cuidado)

```bash
cd infra
terraform init -backend-config="bucket=<state-bucket>"
terraform plan -var-file=prod.tfvars -out=tfplan
# Revisar o plan SEMPRE antes de aplicar
terraform apply tfplan
```

Em produção, o `terraform apply` é disparado pelo workflow `cd-deploy.yml` em manual dispatch (não automático em merge).

## Módulos

```
infra/modules/
├── artifact_registry/      # Container images por pipeline
├── bigquery/               # Datasets, tabelas, schemas JSON, partição/cluster
│   └── schemas/            # JSON schemas versionados (raw + silver críticos)
├── cloud_run/              # Cloud Run Jobs e Services
├── cloud_scheduler/        # Crons que disparam os Jobs
├── cloud_sql/              # PostgreSQL para metadata
├── cloud_workflows/        # GCP Workflows (opcional, futuro)
├── gcs/                    # Buckets (raw/silver/gold + static frontend)
├── iceberg/                # Catálogo Iceberg (opt-in por dataset)
├── looker_bi/              # Conexão Looker (opcional)
├── mapear_ops/             # Alert policies, log metrics, dashboards
├── memorystore/            # Redis (cache, circuit breaker state)
├── monitoring/             # SLOs, notification channels
├── secret_manager/         # Segredos (Apify, Claude, etc)
└── workload_identity/      # OIDC GitHub → GCP
```

## Decisões locais

1. **State remoto em GCS com versioning.** Locks via GCS, sem precisar de Cloud Storage Locking dedicado.
2. **`prod.tfvars` não vai pro git.** [`prod.tfvars.example`](prod.tfvars.example) documenta as variáveis necessárias. Valores reais ficam em local seguro.
3. **Schemas BQ JSON em vez de Terraform nativo.** Schemas mudam mais frequentemente que a estrutura Terraform; JSON em arquivo é mais ergonômico de manter e diff em PR.
4. **Workload Identity por workflow.** Cada workflow tem seu próprio binding — `cd-build-push` pode push no Artifact Registry mas não modificar BQ; `cd-deploy` pode aplicar Terraform mas não roda jobs.
5. **`tfplan-*` no .gitignore.** Plans efêmeros não vão pro repo.

## Migrations

Mudanças que precisam de coordenação (ex.: rename de coluna no BQ) ficam em [`migrations/`](migrations/) como scripts SQL/Python versionados, com runbook descrevendo a ordem de aplicação.
