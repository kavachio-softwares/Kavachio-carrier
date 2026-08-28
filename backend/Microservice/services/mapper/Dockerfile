FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY services/mapper/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Only the shared/lib modules THIS service needs (computed by scripts/compute_deps.py).
# Editing a shared module rebuilds only the services whose subset includes it.
COPY shared/lib/__init__.py shared/lib/assembler.py shared/lib/auth_deps.py shared/lib/auth_tokens.py shared/lib/auth_utils.py shared/lib/canonical.py ./shared/lib/
COPY shared/lib/clients.py shared/lib/common_app_routes.py shared/lib/common_main.py shared/lib/common_validation_routes.py shared/lib/data_model.py shared/lib/db.py ./shared/lib/
COPY shared/lib/direct_lane.py shared/lib/exporter.py shared/lib/extras.py shared/lib/fingerprint.py shared/lib/ingester.py shared/lib/jobs.py ./shared/lib/
COPY shared/lib/mapping_utils.py shared/lib/observability.py shared/lib/scd2_sql.py shared/lib/settings.py shared/lib/storage.py ./shared/lib/
COPY shared/lib/contract_upload_services ./shared/lib/contract_upload_services
COPY services/mapper/app ./app

ENV PYTHONPATH=/app:/app/shared/lib
ENV SERVICE_PORT=8002
EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8002/health').status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8002"]
