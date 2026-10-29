# crm-ingest in a container.
#
# document-extractor is proprietary and not on PyPI: put its wheel in ./wheels first, e.g.
#   copy D:\text_extraction_and_ocr\dist\document_extractor-0.1.0.dev0-py3-none-any.whl wheels\
# or build a fresh one:
#   python -m pip wheel --no-deps --wheel-dir wheels /path/to/text_extraction_and_ocr
# then:
#   docker build -t crm-ingest .
#   docker run --rm --env-file .env -v "$PWD/input:/data:ro" crm-ingest file /data/contract.docx
FROM python:3.12-slim

# Tesseract (English + Arabic) for the OCR used by the "balanced"/"accurate" modes.
# Drop this layer and set CRM_EXTRACTION_MODE=fast for a smaller, text-layer-only image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src

# The extraction library first (with its OCR extra), from the local wheel directory.
COPY wheels/ /wheels/
RUN pip install --find-links /wheels "document-extractor[ocr]"

# Then this project, with msal for SharePoint client-credentials auth.
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --find-links /wheels ".[msal]" \
    && rm -rf /wheels /src

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app
USER app
WORKDIR /data

ENTRYPOINT ["crm-ingest"]
CMD ["--help"]
