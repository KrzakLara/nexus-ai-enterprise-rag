FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    HF_HOME=/models
# Tesseract 5 + English and Croatian language data for OCR of scanned PDFs (Phase 3).
RUN apt-get update  && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-hrv  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.7
# Dependencies first (cached layer), exactly as pinned in uv.lock; CPU-only torch.
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --no-dev --no-install-project
COPY alembic.ini /app/alembic.ini
COPY backend /app/backend
COPY scripts /app/scripts
COPY demo_documents /app/demo_documents
COPY eval /app/eval
RUN uv sync --locked --no-dev
# Phase 4: run as an unprivileged user. Writable paths: the model cache and the local document
# directory (local storage backend / legacy v0.3 documents; the s3 backend writes to MinIO / S3).
RUN groupadd --system --gid 10001 nexus && useradd --system --uid 10001 --gid nexus --home /app nexus \
 && mkdir -p /models /app/data/uploads && chown -R nexus:nexus /models /app/data
USER nexus
EXPOSE 8000 9100
# Default role: API server. Docker Compose runs the same image as `migrate` (one-shot: Alembic,
# runtime roles, demo seed) and as `worker` (python -m backend.app.worker).
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
