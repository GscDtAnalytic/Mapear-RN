.PHONY: install dev dev-api dev-ui build docker-build docker-push docker-run deploy

GCP_PROJECT ?= your-gcp-project
REGION      ?= southamerica-east1
GAR_REPO    ?= mapear-rn
IMAGE_TAG   ?= local
IMAGE       := $(REGION)-docker.pkg.dev/$(GCP_PROJECT)/$(GAR_REPO)/dashboard
SERVICE     ?= mapear-dashboard
SA          ?= looker-bi-reader@$(GCP_PROJECT).iam.gserviceaccount.com

# Workspace root (uv resolves the lockfile from there).
ROOT := ../..

install:
	cd $(ROOT) && uv sync --all-packages --all-extras
	cd frontend && npm install

dev-api:
	GCP_PROJECT_ID=$(GCP_PROJECT) uv run --package mapear-dashboard \
	  uvicorn api.main:app --reload --port 8080

dev-ui:
	cd frontend && npm run dev

dev:
	@echo "Start two terminals:"
	@echo "  make dev-api   (FastAPI on :8080)"
	@echo "  make dev-ui    (Vite on :5173 → proxies /api to :8080)"

build:
	cd frontend && npm run build

docker-build:
	docker build -t $(IMAGE):$(IMAGE_TAG) -f Dockerfile .

docker-push: docker-build
	gcloud auth configure-docker $(REGION)-docker.pkg.dev --quiet
	docker push $(IMAGE):$(IMAGE_TAG)

docker-run:
	docker run --rm -p 8080:8080 \
	  -e GCP_PROJECT_ID=$(GCP_PROJECT) \
	  -v ~/.config/gcloud:/root/.config/gcloud \
	  $(IMAGE):$(IMAGE_TAG)

# Deploy uses the image already pushed to Artifact Registry by either
# `make docker-push` or by the `cd-build-push` workflow.
# Override IMAGE_TAG to deploy a specific commit (e.g. IMAGE_TAG=abc1234).
deploy:
	gcloud run deploy $(SERVICE) \
	  --image=$(IMAGE):$(IMAGE_TAG) \
	  --region=$(REGION) \
	  --platform=managed \
	  --service-account=$(SA) \
	  --set-env-vars=GCP_PROJECT_ID=$(GCP_PROJECT) \
	  --set-secrets=MAPEAR_LLM_API_KEY=mapear-llm-api-key:latest \
	  --no-allow-unauthenticated \
	  --memory=2Gi \
	  --cpu=2 \
	  --min-instances=0 \
	  --max-instances=3
