# Kavachio backend (FastAPI) — single application container.
# Build context: backend/python-services
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Native build deps for psycopg2, PyMuPDF, bcrypt, python-jose[cryptography].
# tesseract-ocr (+ English data) is REQUIRED for OCR of scanned/image PDFs:
# PyMuPDF's get_textpage_ocr shells out to Tesseract, and without it scanned
# contracts extract no text and fail upload with INSUFFICIENT_TEXT. tessdata
# lands in /usr/share/tesseract-ocr/5/tessdata, which document_extractors
# auto-detects for TESSDATA_PREFIX.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl \
        tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as a non-root user.
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Health: App Gateway probes /health if present, else falls back to TCP:8000.
# The app reads all config from environment variables (settings.py / os.getenv),
# which are supplied via docker-compose env_file (/opt/kavachio/.env on the VM).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
