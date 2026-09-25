# AIVA backend API (FastAPI + embedding_service + llm_service)
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt ./
COPY backend/requirements.txt ./backend-requirements.txt
COPY embedding_service/requirements.txt ./embedding-requirements.txt
RUN pip install --upgrade pip \
    && pip install -r backend-requirements.txt -r requirements.txt -r embedding-requirements.txt \
    && pip install email-validator

COPY llm_service/ ./llm_service/
RUN pip install "./llm_service[openai]"

# Document intelligence (optional). With the default WITH_DOC_INTEL=0 nothing is installed.
# --build-arg WITH_DOC_INTEL=1 adds:
# - Tesseract OCR with Arabic and English data;
# - the proprietary document-extractor wheel from wheels/, pinned and never looked up in a
#   package index;
# - its runtime dependencies;
# - crm-document-ingestion from this repository (the SharePoint sync), with --no-deps: its
#   dependencies are installed above and by backend/requirements.txt.
# Put document_extractor-0.1.0.dev0-py3-none-any.whl in wheels/ before building.
ARG WITH_DOC_INTEL=0
COPY wheels/ /tmp/wheels/
COPY crm-document-ingestion/ /tmp/crm-document-ingestion/
COPY backend/requirements-docintel.txt ./docintel-requirements.txt
RUN if [ "$WITH_DOC_INTEL" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng \
        && rm -rf /var/lib/apt/lists/* \
        && pip install --no-index --no-deps --find-links /tmp/wheels "document-extractor==0.1.0.dev0" \
        && pip install -r docintel-requirements.txt \
        && pip install --no-deps /tmp/crm-document-ingestion; \
    fi \
    && rm -rf /tmp/wheels /tmp/crm-document-ingestion

COPY backend/ ./backend/
COPY embedding_service/ ./embedding_service/
COPY zoho_auth/ ./zoho_auth/

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "-m", "backend.main"]
