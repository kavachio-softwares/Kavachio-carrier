"""
Phase 2 + 3 wiring.

- Rewrites each service's app/main.py to serve its real slice of routes via the
  strangler splitter in shared/core/service_split.py.
- Gives every service the full (union) requirements so it can import the core.
- Updates Dockerfiles (PYTHONPATH), the nginx gateway (route ownership), the
  docker-compose stack (adds Redis for Phase 3 jobs), and the .env template.
"""
import os

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"


def write(rel, content):
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content.lstrip("\n"))
    print("  ~", rel)


SERVICES = {
    "auth": 8001,
    "tenant-admin": 8007,
    "mapper": 8002,
    "ingestion": 8003,
    "validation": 8004,
    "export": 8005,
    "contract": 8006,
}

# Full union requirements (the strangler imports the whole core, so every service
# needs every dependency). Read from the core requirements + Phase 3 extras.
with open(os.path.join(ROOT, "shared/core/requirements.txt")) as f:
    core_reqs = f.read().strip()

UNION_REQS = core_reqs + "\n\n# --- Phase 3 extras ---\nredis==5.0.8\n"

print("Wiring Phase 2 + 3...\n")

# ------------------------------------------------------------------
# Per-service main.py (strangler) + requirements + Dockerfile
# ------------------------------------------------------------------
for name, port in SERVICES.items():
    svc_full = f"{name}-service"

    main_py = f'''
"""
{svc_full} (Phase 2).

This service serves its real, working slice of the Kavachio API. The business
logic lives in shared/core (the former monolith); build_service_app() imports it
and keeps only the routes this service owns. See shared/core/service_split.py for
the exact ownership map (mirrored by gateway/nginx.conf).
"""
import os
import sys


def _add_core_to_path():
    """Locate shared/core and put it on sys.path (works locally and in Docker)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        cand = os.path.join(here, "shared", "core")
        if os.path.isdir(cand):
            sys.path.insert(0, cand)
            return
        here = os.path.dirname(here)
    for cand in ("/app/shared/core",):
        if os.path.isdir(cand):
            sys.path.insert(0, cand)
            return
    raise RuntimeError("could not locate shared/core")


_add_core_to_path()

from service_split import build_service_app  # noqa: E402

SERVICE_NAME = "{svc_full}"
SERVICE_PORT = {port}

app = build_service_app(SERVICE_NAME, title="Kavachio {svc_full}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=SERVICE_PORT, reload=True)
'''
    write(f"services/{name}/app/main.py", main_py)
    write(f"services/{name}/requirements.txt", UNION_REQS + "\n")

    dockerfile = f'''
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gcc libpq-dev \\
 && rm -rf /var/lib/apt/lists/*

COPY services/{name}/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Full business core (shared) + this service's thin entrypoint.
COPY shared ./shared
COPY services/{name}/app ./app

# /app so `app.main` imports; /app/shared/core so the core modules import.
ENV PYTHONPATH=/app:/app/shared/core
ENV SERVICE_PORT={port}
EXPOSE {port}

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:{port}/health').status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port {port}"]
'''
    write(f"services/{name}/Dockerfile", dockerfile)

# ------------------------------------------------------------------
# Gateway - nginx routing mirrors service_split.py ownership
# ------------------------------------------------------------------
nginx_conf = '''
worker_processes auto;
events { worker_connections 1024; }

http {
    sendfile on;
    keepalive_timeout 65;
    client_max_body_size 100M;   # large Excel / PDF uploads

    upstream auth_service        { server auth-service:8001; }
    upstream tenant_admin_service{ server tenant-admin-service:8007; }
    upstream mapper_service      { server mapper-service:8002; }
    upstream ingestion_service   { server ingestion-service:8003; }
    upstream validation_service  { server validation-service:8004; }
    upstream export_service      { server export-service:8005; }
    upstream contract_service    { server contract-service:8006; }

    # Shared proxy headers (X-Request-ID lets us trace across services)
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Request-ID $request_id;

    server {
        listen 80;

        location = /gateway/health { return 200 'gateway ok'; add_header Content-Type text/plain; }

        # ---- REGEX locations first (most specific; nginx gives regex priority) ----
        # Contracts are nested under /programs but owned by contract-service.
        location ~ ^/programs/[^/]+/contracts { proxy_pass http://contract_service; }
        # Exception decision on a generated export is owned by validation-service.
        location ~ ^/export/downloads/[^/]+/decide { proxy_pass http://validation_service; }

        # ---- PREFIX locations (nginx picks the longest match) ----
        # auth-service
        location /auth      { proxy_pass http://auth_service; }
        location /users     { proxy_pass http://auth_service; }

        # tenant-admin-service
        location /tenants    { proxy_pass http://tenant_admin_service; }
        location /parties    { proxy_pass http://tenant_admin_service; }
        location /programs   { proxy_pass http://tenant_admin_service; }
        location /onboarding { proxy_pass http://tenant_admin_service; }
        location /dashboard  { proxy_pass http://tenant_admin_service; }
        location /activity   { proxy_pass http://tenant_admin_service; }

        # mapper-service
        location /mapper        { proxy_pass http://mapper_service; }   # also /mappers/*
        location /data-model    { proxy_pass http://mapper_service; }
        location /extra-fields  { proxy_pass http://mapper_service; }
        location /bdx/sheets    { proxy_pass http://mapper_service; }
        location /bdx/preview   { proxy_pass http://mapper_service; }
        location /api/canonical { proxy_pass http://mapper_service; }

        # ingestion-service
        location /bdx/upload          { proxy_pass http://ingestion_service; }
        location /uploads             { proxy_pass http://ingestion_service; }
        location /dwh                 { proxy_pass http://ingestion_service; }
        location /direct              { proxy_pass http://ingestion_service; }
        location /admin/mapping-tasks { proxy_pass http://ingestion_service; }

        # validation-service
        location /api/validate { proxy_pass http://validation_service; }

        # export-service (catch the rest of /export*)
        location /export { proxy_pass http://export_service; }
    }
}
'''
write("gateway/nginx.conf", nginx_conf)

# ------------------------------------------------------------------
# docker-compose (adds Redis for Phase 3 jobs)
# ------------------------------------------------------------------
compose_services = []
for name, port in SERVICES.items():
    compose_services.append(f'''
  {name}-service:
    build:
      context: ..
      dockerfile: services/{name}/Dockerfile
    image: kavachio/{name}-service:local
    env_file: .env
    environment:
      SERVICE_PORT: "{port}"
    ports:
      - "{port}:{port}"
    depends_on:
      postgres:
        condition: service_healthy
      minio:
        condition: service_started
      redis:
        condition: service_started
    restart: unless-stopped
''')

docker_compose = f'''
# Kavachio - local microservices stack (Phase 2 + 3)
# Usage:  cd infra && cp .env.example .env && docker compose up --build
name: kavachio

services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: kavachio
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres123
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d kavachio"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin123
    ports:
      - "9000:9000"
      - "9001:9001"
    volumes:
      - miniodata:/data
{''.join(compose_services)}
  gateway:
    build:
      context: ..
      dockerfile: gateway/Dockerfile
    image: kavachio/gateway:local
    ports:
      - "8080:80"
    depends_on:
{''.join(f'      - {n}-service' + chr(10) for n in SERVICES)}    restart: unless-stopped

volumes:
  pgdata:
  miniodata:
'''
write("infra/docker-compose.yml", docker_compose)

# ------------------------------------------------------------------
# .env template (real variable names used by settings.py / db.py)
# ------------------------------------------------------------------
env_example = '''
# Shared config for the local microservices stack (infra/docker-compose.yml).
# Copy to infra/.env and set real secrets.  Do NOT commit .env.

# --- Shared database (Phase 1: one DB for all services) ---
DATABASE_URL=postgresql+psycopg2://postgres:postgres123@postgres:5432/kavachio

# --- Auth (names match shared/core/settings.py) ---
JWT_SECRET=change-me-to-a-long-random-string
JWT_ALG=HS256
JWT_ISSUER=kavachio-auth
ACCESS_TTL_MIN=60
REFRESH_TTL_DAYS=7

# --- Phase 3: background jobs ---
REDIS_URL=redis://redis:6379/0
JOB_WORKERS=4

# --- Shared file store (MinIO / S3) ---
S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin123
S3_BUCKET=kavachio-uploads

# --- LLM ---
GEMINI_API_KEY=

# --- Tracing (optional; set an OTLP endpoint to enable OpenTelemetry) ---
# OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

# --- CORS ---
CORS_ORIGINS=http://localhost:5173,http://localhost:8080
'''
write("infra/.env.example", env_example)

# ------------------------------------------------------------------
# k8s: Redis for Phase 3
# ------------------------------------------------------------------
write("k8s/base/redis.yaml", '''
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis
  namespace: kavachio
spec:
  replicas: 1
  selector:
    matchLabels: { app: redis }
  template:
    metadata:
      labels: { app: redis }
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          ports:
            - containerPort: 6379
---
apiVersion: v1
kind: Service
metadata:
  name: redis
  namespace: kavachio
spec:
  selector: { app: redis }
  ports:
    - port: 6379
      targetPort: 6379
''')

print("\nDone.")
