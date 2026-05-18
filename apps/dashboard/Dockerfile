FROM node:20-slim AS frontend-builder
WORKDIR /build
COPY frontend/package.json ./
RUN npm install --frozen-lockfile 2>/dev/null || npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    HF_HOME=/app/.hf_cache

WORKDIR /app

# Python deps — torch CPU-only primeiro: evita baixar o build CUDA (~2GB)
# que a Cloud Run (sem GPU) nunca usaria.
COPY api/requirements.txt ./requirements.txt
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt

# Pré-baixa o modelo de embedding para dentro da imagem — sem download
# em runtime (cold-start previsível na Cloud Run).
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('paraphrase-multilingual-mpnet-base-v2')"

# API source
COPY api/ ./api/

# React build
COPY --from=frontend-builder /build/dist ./frontend/dist

# Run FastAPI
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
