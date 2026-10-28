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

# Corpus index, built into the image (docs/decisions.md D-018): parse the PDFs, chunk,
# download the embedding model into data/models/ and embed. This layer is rebuilt only
# when the PDFs, the ingest code or EMBED_MODEL change; `ingest status` fails the build
# if the result is not fresh.
ARG EMBED_MODEL=BAAI/bge-small-en-v1.5
ENV EMBED_MODEL=${EMBED_MODEL}
COPY contracts contracts
COPY common common
COPY ingest ingest
COPY corpus/README.md corpus/README.md
COPY corpus/raw corpus/raw
RUN python -m ingest build && python -m ingest status \
    && chown -R app:app data
# The model is in the image; never download at run time.
ENV HF_HUB_OFFLINE=1

COPY retrieval retrieval
COPY agent agent
COPY worker worker

USER app
CMD ["python", "-m", "worker.main"]
