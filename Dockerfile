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

COPY backend/ ./backend/
COPY embedding_service/ ./embedding_service/
COPY zoho_auth/ ./zoho_auth/

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["python", "-m", "backend.main"]
