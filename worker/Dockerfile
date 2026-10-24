# Build context: repository root (see docker-compose.yml).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 10001 app

COPY worker/requirements.txt worker/requirements.txt
RUN pip install -r worker/requirements.txt

COPY contracts contracts
COPY common common
COPY agent agent
COPY worker worker

USER app
CMD ["python", "-m", "worker.main"]
