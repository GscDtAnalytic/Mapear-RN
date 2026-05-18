#!/usr/bin/env bash
# ==============================================================================
# Mapear-RSS — Bootstrap GCP Project
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
GITHUB_REPO="mapear-rss"

echo "========================================"
echo "  Mapear-RSS — GCP Bootstrap"
echo "  Project: ${PROJECT_ID}"
echo "  Region:  ${REGION}"
echo "========================================"
echo ""

# --- 1. Configurar projeto ---
echo "[1/7] Configurando projeto GCP..."
gcloud config set project "${PROJECT_ID}"

# --- 2. Habilitar APIs necessárias ---
echo "[2/7] Habilitando APIs..."
APIS=(
  "compute.googleapis.com"                  # VPC, Firewall
  "iam.googleapis.com"                      # IAM, Service Accounts
  "iamcredentials.googleapis.com"           # WIF token exchange
  "sts.googleapis.com"                      # Security Token Service (WIF)
  "cloudresourcemanager.googleapis.com"     # Project IAM
  "artifactregistry.googleapis.com"         # Docker images
  "run.googleapis.com"                      # Cloud Run
  "sqladmin.googleapis.com"                 # Cloud SQL
  "redis.googleapis.com"                    # Memorystore
  "bigquery.googleapis.com"                 # BigQuery
  "storage.googleapis.com"                  # GCS
  "composer.googleapis.com"                 # Cloud Composer
  "secretmanager.googleapis.com"            # Secret Manager
  "servicenetworking.googleapis.com"        # VPC peering (Cloud SQL)
  "cloudresourcemanager.googleapis.com"     # Resource Manager
)

for api in "${APIS[@]}"; do
  echo "  Enabling ${api}..."
  gcloud services enable "${api}" --quiet
done

# --- 3. Criar bucket para Terraform state ---
echo "[3/7] Criando bucket para Terraform state..."
if gsutil ls -b "gs://${TF_STATE_BUCKET}" 2>/dev/null; then
  echo "  Bucket ${TF_STATE_BUCKET} já existe"
else
  gsutil mb -l "${REGION}" -p "${PROJECT_ID}" "gs://${TF_STATE_BUCKET}"
  gsutil versioning set on "gs://${TF_STATE_BUCKET}"
  echo "  Bucket ${TF_STATE_BUCKET} criado com versionamento"
fi

# --- 4. Criar Artifact Registry repository ---
echo "[4/7] Criando Artifact Registry..."
if gcloud artifacts repositories describe mapear-rss \
    --location="${REGION}" --project="${PROJECT_ID}" 2>/dev/null; then
  echo "  Repository mapear-rss já existe"
else
  gcloud artifacts repositories create mapear-rss \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --description="Mapear-RSS pipeline images"
  echo "  Repository mapear-rss criado"
fi

# --- 5. Gerar Fernet key para Airflow ---
echo "[5/7] Gerando Airflow Fernet key..."
FERNET_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || echo "INSTALL_CRYPTOGRAPHY_TO_GENERATE")
echo "  Fernet key: ${FERNET_KEY}"
echo "  (salve em infra/prod.tfvars como airflow_fernet_key)"

# --- 6. Gerar senha do banco ---
echo "[6/7] Gerando senha do PostgreSQL..."
DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 32)
echo "  DB password: ${DB_PASSWORD}"
echo "  (salve em infra/prod.tfvars como db_password)"

# --- 7. Configurar Terraform backend ---
echo "[7/7] Configurando Terraform backend..."
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
echo "     # Edite db_password e airflow_fernet_key"
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
