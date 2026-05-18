#!/usr/bin/env bash
# Deploy manual de imagens Docker + atualização do Cloud Run via Terraform.
# Uso: ./scripts/deploy_manual.sh [rss|social|dbt|graph|nlp|all]
#
# Variáveis de ambiente opcionais:
#   SKIP_IAM_CHECK=1     — pula a verificação de permissão IAM
#   SKIP_TERRAFORM=1     — pula o terraform apply (só build+push)
#   DRY_RUN=1            — mostra comandos sem executar

set -euo pipefail

# --- Constantes ---
readonly GCP_PROJECT="your-gcp-project"
readonly GAR_REGION="southamerica-east1"
readonly GAR_REPO="mapear-rn"
readonly GAR_HOST="${GAR_REGION}-docker.pkg.dev"
readonly IMAGE_BASE="${GAR_HOST}/${GCP_PROJECT}/${GAR_REPO}"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly INFRA_DIR="${REPO_ROOT}/infra"

# --- Helpers ---
log()  { echo "[deploy] $*"; }
err()  { echo "[deploy] ERROR: $*" >&2; exit 1; }
run()  {
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "  DRY: $*"
  else
    "$@"
  fi
}

# --- Detecção de plataforma ---
PLATFORM_FLAG=""
if [[ "$(uname -m)" == "arm64" ]]; then
  log "ARM detectado — adicionando --platform linux/amd64"
  PLATFORM_FLAG="--platform linux/amd64"
fi

# --- SHA do commit atual ---
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"

# --- Mapeamento service → (image_name, dockerfile) ---
declare -A IMAGE_NAME DOCKERFILE
IMAGE_NAME[rss]="rss-pipeline"
IMAGE_NAME[social]="social-pipeline"
IMAGE_NAME[dbt]="dbt-runner"
IMAGE_NAME[graph]="graph-runner"
IMAGE_NAME[nlp]="nlp-runner"
IMAGE_NAME[alert]="alert-runner"
IMAGE_NAME[embed-social]="embed-social-runner"

DOCKERFILE[rss]="Mapear-RSS/Dockerfile"
DOCKERFILE[social]="mapear-social/Dockerfile"
DOCKERFILE[dbt]="scripts/dbt_runner/Dockerfile"
DOCKERFILE[graph]="scripts/graph_runner/Dockerfile"
DOCKERFILE[nlp]="scripts/nlp_runner/Dockerfile"
DOCKERFILE[alert]="scripts/alert_runner/Dockerfile"
DOCKERFILE[embed-social]="scripts/embed_social_runner/Dockerfile"

# --- Argumento ---
SERVICE="${1:-}"
if [[ -z "${SERVICE}" ]]; then
  err "Informe o serviço: rss | social | dbt | graph | all"
fi

if [[ "${SERVICE}" != "all" ]] && [[ -z "${IMAGE_NAME[${SERVICE}]+x}" ]]; then
  err "Serviço desconhecido: '${SERVICE}'. Use: rss, social, dbt, graph, nlp, alert, embed-social, all"
fi

# --- 1. Verificação IAM ---
if [[ "${SKIP_IAM_CHECK:-0}" != "1" ]]; then
  log "Verificando permissão roles/artifactregistry.writer no projeto ${GCP_PROJECT}..."
  run gcloud projects get-iam-policy "${GCP_PROJECT}" \
    --flatten="bindings[].members" \
    --filter="bindings.role=roles/artifactregistry.writer" \
    --format="table(bindings.members)"
fi

# --- 2. Autenticação Docker ---
log "Configurando autenticação Docker para ${GAR_HOST}..."
run gcloud auth configure-docker "${GAR_HOST}" --quiet

# --- Função de build+push de um serviço ---
build_push() {
  local svc="$1"
  local image="${IMAGE_BASE}/${IMAGE_NAME[${svc}]}"
  local df="${REPO_ROOT}/${DOCKERFILE[${svc}]}"

  log "--- ${svc} ---"
  log "Imagem : ${image}"
  log "Dockerfile: ${df}"

  if [[ ! -f "${df}" ]]; then
    err "Dockerfile não encontrado: ${df}"
  fi

  # Build
  log "Build..."
  run docker build \
    ${PLATFORM_FLAG} \
    --tag "${image}:${GIT_SHA}" \
    --tag "${image}:latest" \
    -f "${df}" \
    "${REPO_ROOT}"

  # Push
  log "Push :${GIT_SHA}..."
  run docker push "${image}:${GIT_SHA}"
  log "Push :latest..."
  run docker push "${image}:latest"

  log "${svc} enviado: ${image}:${GIT_SHA}"
}

# --- 3. Build + Push ---
if [[ "${SERVICE}" == "all" ]]; then
  for svc in rss social dbt graph nlp alert embed-social; do
    build_push "${svc}"
  done
else
  build_push "${SERVICE}"
fi

# --- 4. Terraform apply ---
if [[ "${SKIP_TERRAFORM:-0}" != "1" ]]; then
  if [[ ! -f "${INFRA_DIR}/prod.tfvars" ]]; then
    log "AVISO: ${INFRA_DIR}/prod.tfvars não encontrado — pulando terraform apply."
    log "Crie o arquivo a partir de prod.tfvars.example e rode:"
    log "  cd infra && terraform apply -var-file=prod.tfvars -target=module.cloud_run"
  else
    log "Atualizando Cloud Run Jobs via Terraform..."
    run terraform -chdir="${INFRA_DIR}" apply \
      -var-file=prod.tfvars \
      -target=module.cloud_run \
      -auto-approve
  fi
fi

log "Deploy manual concluído."
