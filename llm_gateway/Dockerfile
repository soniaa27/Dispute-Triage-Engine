# Build context: repository root (see docker-compose.yml).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 10001 app

COPY llm_gateway/requirements.txt llm_gateway/requirements.txt
RUN pip install -r llm_gateway/requirements.txt

COPY common/__init__.py common/logging.py common/
COPY llm_gateway llm_gateway

USER app
EXPOSE 8100
CMD ["uvicorn", "--factory", "llm_gateway.main:create_app", "--host", "0.0.0.0", "--port", "8100"]
