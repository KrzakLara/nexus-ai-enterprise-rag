# Linux test runner with the same toolchain as CI (Python 3.12, uv 0.13.7, Tesseract eng+hrv).
# Used to run the full suite with OCR on machines without a local Tesseract (e.g. Windows):
#
#   docker build -f docker/test-runner.Dockerfile -t nexus-test-runner .
#   docker run --rm --network <net-with-postgres-minio-keycloak> -v "$PWD:/src" \
#     -e TEST_DATABASE_URL=postgresql+psycopg://nexus:<pw>@postgres:5432/nexus_test \
#     -e TEST_S3_ENDPOINT_URL=http://minio:9000 -e TEST_KEYCLOAK_URL=... nexus-test-runner
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-hrv \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.7
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy PATH=/opt/venv/bin:$PATH HF_HOME=/hf PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
COPY pyproject.toml uv.lock /src/
RUN uv sync --locked --extra dev --no-install-project
CMD ["sh", "-c", "uv sync --locked --extra dev >/dev/null && python -m pytest -q -rs -p no:cacheprovider --cov=backend/app --cov-report=term"]
