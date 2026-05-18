#!/usr/bin/env bash
# ==============================================================================
# Mapear-RN — Bootstrap GCP Project
#
# Executa a configuração inicial do projeto GCP do zero.
# Roda UMA VEZ antes do primeiro terraform apply.
#
# Pré-requisitos:
#   - gcloud CLI instalado e autenticado (gcloud auth login)
#   - Conta com permissão de Owner no projeto
#   - Billing account vinculado ao projeto
#
# Uso:
#   chmod +x scripts/bootstrap_gcp.sh
#   ./scripts/bootstrap_gcp.sh
# ==============================================================================

set -euo pipefail

# --- Configuração ---
PROJECT_ID="your-gcp-project"
REGION="southamerica-east1"
TF_STATE_BUCKET="${PROJECT_ID}-terraform-state"
GITHUB_ORG="your-github-org"
GITHUB_REPO="Mapear-RN"

echo "========================================"
echo "  Mapear-RN — GCP Bootstrap"
echo "  Project: ${PROJECT_ID}"
echo "  Region:  ${REGION}"
echo "========================================"
echo ""

# --- 1. Configurar projeto ---
echo "[1/6] Configurando projeto GCP..."
gcloud config set project "${PROJECT_ID}"

# --- 2. Habilitar APIs necessárias ---
echo "[2/6] Habilitando APIs..."
APIS=(
  "compute.googleapis.com"                  # VPC, Firewall
  "iam.googleapis.com"                      # IAM, Service Accounts
  "iamcredentials.googleapis.com"           # WIF token exchange
  "sts.googleapis.com"                      # Security Token Service (WIF)
  "cloudresourcemanager.googleapis.com"     # Project IAM
  "artifactregistry.googleapis.com"         # Docker images
  "run.googleapis.com"                      # Cloud Run Jobs
  "sqladmin.googleapis.com"                 # Cloud SQL
  "redis.googleapis.com"                    # Memorystore
  "bigquery.googleapis.com"                 # BigQuery
  "storage.googleapis.com"                  # GCS
  "cloudscheduler.googleapis.com"           # Cloud Scheduler (cron triggers)
  "secretmanager.googleapis.com"            # Secret Manager
  "servicenetworking.googleapis.com"        # VPC peering (Cloud SQL)
  "vpcaccess.googleapis.com"               # Serverless VPC Access Connector
)

for api in "${APIS[@]}"; do
  echo "  Enabling ${api}..."
  gcloud services enable "${api}" --quiet
done

# --- 3. Criar bucket para Terraform state ---
echo "[3/6] Criando bucket para Terraform state..."
if gsutil ls -b "gs://${TF_STATE_BUCKET}" 2>/dev/null; then
  echo "  Bucket ${TF_STATE_BUCKET} já existe"
else
  gsutil mb -l "${REGION}" -p "${PROJECT_ID}" "gs://${TF_STATE_BUCKET}"
  gsutil versioning set on "gs://${TF_STATE_BUCKET}"
  echo "  Bucket ${TF_STATE_BUCKET} criado com versionamento"
fi

# --- 4. Criar Artifact Registry repository ---
echo "[4/6] Criando Artifact Registry..."
if gcloud artifacts repositories describe mapear-rn \
    --location="${REGION}" --project="${PROJECT_ID}" 2>/dev/null; then
  echo "  Repository mapear-rn já existe"
else
  gcloud artifacts repositories create mapear-rn \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --description="Mapear-RN pipeline images (RSS, Social, dbt)"
  echo "  Repository mapear-rn criado"
fi

# --- 5. Gerar senha do banco ---
echo "[5/6] Gerando senha do PostgreSQL..."
DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
echo "  DB password: ${DB_PASSWORD}"
echo "  (salve em infra/prod.tfvars como db_password)"

# --- 6. Configurar Terraform backend ---
echo "[6/6] Configurando Terraform backend..."
cat > infra/backend.tf << TFEOF
terraform {
  backend "gcs" {
    bucket = "${TF_STATE_BUCKET}"
    prefix = "terraform/state"
  }
}
TFEOF
echo "  Backend configurado em infra/backend.tf"

echo ""
echo "========================================"
echo "  Bootstrap concluído!"
echo "========================================"
echo ""
echo "Próximos passos:"
echo ""
echo "  1. Salve as credenciais geradas em infra/prod.tfvars:"
echo "     cp infra/prod.tfvars.example infra/prod.tfvars"
echo "     # Edite db_password e demais variáveis sensíveis"
echo ""
echo "  2. Inicialize e aplique o Terraform:"
echo "     cd infra"
echo "     terraform init"
echo "     terraform plan -var-file=prod.tfvars"
echo "     terraform apply -var-file=prod.tfvars"
echo ""
echo "  3. Após terraform apply, configure GitHub:"
echo "     - Vá em https://github.com/${GITHUB_ORG}/${GITHUB_REPO}/settings/variables/actions"
echo "     - Adicione as variáveis (Repository Variables, não Secrets):"
echo ""
echo "       WIF_PROVIDER = (valor do output 'workload_identity_provider')"
echo "       CI_SERVICE_ACCOUNT = (valor do output 'ci_service_account')"
echo "       CD_SERVICE_ACCOUNT = (valor do output 'cd_service_account')"
echo ""
echo "  4. Configure o environment 'production' em:"
echo "     https://github.com/${GITHUB_ORG}/${GITHUB_REPO}/settings/environments"
echo "     - Adicione required reviewers para approval manual"
echo ""
echo "  5. Faça o primeiro push para main:"
echo "     git push origin main"
echo ""
