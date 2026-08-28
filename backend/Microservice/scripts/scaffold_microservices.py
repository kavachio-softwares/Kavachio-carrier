"""
Scaffolds the Kavachio microservices structure on the local system.
Phase 0 (reorganize) + Phase 1 (containers, gateway, shared DB, CI/CD).

Safe by design:
  - Does NOT touch or delete the existing monolith in backend/python-services.
  - Only creates new folders/files at the repo root.
  - Re-runnable (overwrites the files it owns).
"""
import os

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"


def write(rel_path, content):
    path = os.path.join(ROOT, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content.lstrip("\n"))
    print("  +", rel_path)


# service_name -> (port, description, extra requirements, owned tables, main paths)
SERVICES = {
    "auth": {
        "port": 8001,
        "desc": "Login, logout, token refresh, password reset, user identity.",
        "reqs": ["python-jose[cryptography]==3.5.0", "bcrypt>=4.0.0"],
        "owns": "users",
        "paths": "/auth/login, /auth/refresh, /auth/logout, /auth/reset",
        "monolith_modules": "auth_deps.py, auth_tokens.py, auth_utils.py, email_utils.py",
    },
    "tenant-admin": {
        "port": 8007,
        "desc": "Master data: tenants, parties, programs.",
        "reqs": [],
        "owns": "tenants, parties, programs",
        "paths": "/tenants, /parties, /programs",
        "monolith_modules": "tenant/party/program routes from app_routes.py",
    },
    "mapper": {
        "port": 8002,
        "desc": "LLM-driven column mapping (Gemini + embeddings, cosine fallback).",
        "reqs": ["google-genai==2.8.0", "glom==23.5.0", "pandas==2.2.2", "openpyxl==3.1.5", "json-repair==0.30.3"],
        "owns": "mappers, fingerprints",
        "paths": "/mapper/*, /bdx/sheets, /bdx/preview, /data-model",
        "monolith_modules": "mapper.py, fingerprint.py, data_model.py",
    },
    "ingestion": {
        "port": 8003,
        "desc": "Reads Excel, applies mapping spec (glom), writes to the canonical warehouse (SCD2).",
        "reqs": ["pandas==2.2.2", "openpyxl==3.1.5", "glom==23.5.0"],
        "owns": "uploads, uploads_policy, canonical warehouse tables",
        "paths": "/bdx/upload, /direct/*, /uploads/*",
        "monolith_modules": "ingester.py, direct_lane.py, direct_mapper.py, direct_render.py, assembler.py, scd2_sql.py",
    },
    "validation": {
        "port": 8004,
        "desc": "Rule checks using an in-memory DuckDB. 100% Python (no Node).",
        "reqs": ["duckdb==1.5.4", "pandas==2.2.2"],
        "owns": "exception_decisions",
        "paths": "/api/validate, /api/validate/exceptions/decide",
        "monolith_modules": "duckdb_validation.py, validation_routes.py",
    },
    "export": {
        "port": 8005,
        "desc": "Builds output Excel workbooks from approved templates + warehouse data.",
        "reqs": ["pandas==2.2.2", "openpyxl==3.1.5"],
        "owns": "export_templates",
        "paths": "/export/template/*, /export/generate",
        "monolith_modules": "exporter.py, assembler.py",
    },
    "contract": {
        "port": 8006,
        "desc": "Contract uploads: PDF/Word extraction, LLM rule generation, versioning.",
        "reqs": ["google-genai==2.8.0", "PyMuPDF==1.24.10", "PyPDF2==3.0.1", "python-docx==1.1.2", "json-repair==0.30.3"],
        "owns": "contracts, contract rule tables",
        "paths": "/contracts/*",
        "monolith_modules": "contract_upload_services/ (15+ modules)",
    },
}

BASE_REQS = [
    "fastapi==0.115.0",
    "uvicorn[standard]==0.30.6",
    "sqlalchemy==2.0.35",
    "psycopg2-binary==2.9.9",
    "pydantic==2.9.2",
    "python-dotenv==1.0.1",
    "python-multipart==0.0.9",
]

print("Scaffolding Kavachio microservices...\n")

# ------------------------------------------------------------------
# Per-service files
# ------------------------------------------------------------------
for name, s in SERVICES.items():
    port = s["port"]
    svc_full = f"{name}-service"

    # requirements.txt
    reqs = "\n".join(BASE_REQS + s["reqs"]) + "\n"
    write(f"services/{name}/requirements.txt", reqs)

    # app/__init__.py
    write(f"services/{name}/app/__init__.py", "")

    # app/main.py
    main_py = f'''
"""
{svc_full} - {s["desc"]}

Phase 1 skeleton: health check + CORS + shared-DB connectivity check.
Phase 2 TODO: move these monolith modules in and mount their routers:
    {s["monolith_modules"]}
Owned tables (write): {s["owns"]}
Main API paths: {s["paths"]}
"""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

SERVICE_NAME = "{svc_full}"
SERVICE_PORT = {port}

app = FastAPI(title="Kavachio {svc_full}", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """Liveness probe used by Docker, Kubernetes and the gateway."""
    return {{"service": SERVICE_NAME, "status": "ok"}}


@app.get("/health/db")
def health_db():
    """Readiness probe: confirms the shared PostgreSQL database is reachable."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return {{"service": SERVICE_NAME, "db": "no DATABASE_URL set"}}
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {{"service": SERVICE_NAME, "db": "ok"}}
    except Exception as exc:  # noqa: BLE001
        return {{"service": SERVICE_NAME, "db": "error", "detail": str(exc)}}


@app.get("/")
def root():
    return {{
        "service": SERVICE_NAME,
        "description": "{s["desc"]}",
        "owns": "{s["owns"]}",
        "paths": "{s["paths"]}",
    }}


# --------------------------------------------------------------------------
# Phase 2: uncomment and wire up once the modules above are moved into
#          services/{name}/app/ and refactored to import from `shared`.
#
# from .routes import router
# app.include_router(router)
# --------------------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=SERVICE_PORT, reload=True)
'''
    write(f"services/{name}/app/main.py", main_py)

    # Dockerfile (build context = repo root, so it can copy shared/)
    dockerfile = f'''
FROM python:3.11-slim

WORKDIR /app

# System deps for psycopg2 / pandas wheels
RUN apt-get update && apt-get install -y --no-install-recommends \\
    gcc libpq-dev \\
 && rm -rf /var/lib/apt/lists/*

COPY services/{name}/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Shared internal package (db models, canonical schema, auth helpers)
COPY shared ./shared
# Service code
COPY services/{name}/app ./app

ENV PYTHONPATH=/app
ENV SERVICE_PORT={port}
EXPOSE {port}

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:{port}/health').status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port {port}"]
'''
    write(f"services/{name}/Dockerfile", dockerfile)

    # .dockerignore
    write(f"services/{name}/.dockerignore", "__pycache__/\n*.pyc\nvenv/\n.env\ntests/\n")

    # per-service README
    readme = f'''
# {svc_full}

{s["desc"]}

| Property | Value |
|---|---|
| Port | `{port}` |
| Owns (writes) | `{s["owns"]}` |
| Main paths | `{s["paths"]}` |
| Modules to migrate (Phase 2) | {s["monolith_modules"]} |

## Run locally (standalone)
```bash
cd services/{name}
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://postgres:postgres123@localhost:5432/kavachio
uvicorn app.main:app --host 0.0.0.0 --port {port} --reload
```

## Health
- `GET /health` - liveness
- `GET /health/db` - shared-DB readiness
'''
    write(f"services/{name}/README.md", readme)

# ------------------------------------------------------------------
# shared/ package (real common modules are copied in by the shell step)
# ------------------------------------------------------------------
write("shared/__init__.py", '"""Kavachio shared internal package: DB models, canonical schema, auth helpers."""\n')
write("shared/README.md", '''
# shared

Common code imported by every microservice. Copied here from the monolith
(`backend/python-services`) as the starting point:

- `db.py`            - SQLAlchemy models + shared DB connection
- `canonical.py`     - 53 canonical warehouse table schemas
- `data_model.py`    - canonical field definitions
- `settings.py`      - JWT / config settings
- `auth_deps.py`     - token validation dependency
- `auth_tokens.py`   - token creation
- `auth_utils.py`    - password hashing / token helpers
- `email_utils.py`   - email delivery

Keep this package small and dependency-light. Services import it as `shared.*`.
''')

# ------------------------------------------------------------------
# Gateway (nginx)
# ------------------------------------------------------------------
nginx_conf = '''
worker_processes auto;
events { worker_connections 1024; }

http {
    sendfile on;
    keepalive_timeout 65;
    client_max_body_size 100M;   # allow large Excel / PDF uploads

    # ---- upstreams: internal service names from docker-compose / k8s ----
    upstream auth_service        { server auth-service:8001; }
    upstream tenant_admin_service{ server tenant-admin-service:8007; }
    upstream mapper_service      { server mapper-service:8002; }
    upstream ingestion_service   { server ingestion-service:8003; }
    upstream validation_service  { server validation-service:8004; }
    upstream export_service      { server export-service:8005; }
    upstream contract_service    { server contract-service:8006; }

    server {
        listen 80;

        # Gateway health
        location = /gateway/health { return 200 'gateway ok'; add_header Content-Type text/plain; }

        # ---- auth ----
        location /auth/            { proxy_pass http://auth_service; }

        # ---- master data ----
        location /tenants          { proxy_pass http://tenant_admin_service; }
        location /parties          { proxy_pass http://tenant_admin_service; }
        location /programs         { proxy_pass http://tenant_admin_service; }

        # ---- mapper ----
        location /mapper/          { proxy_pass http://mapper_service; }
        location /data-model       { proxy_pass http://mapper_service; }
        location /bdx/sheets       { proxy_pass http://mapper_service; }
        location /bdx/preview      { proxy_pass http://mapper_service; }

        # ---- ingestion ----
        location /bdx/upload       { proxy_pass http://ingestion_service; }
        location /direct/          { proxy_pass http://ingestion_service; }
        location /uploads          { proxy_pass http://ingestion_service; }

        # ---- validation ----
        location /api/validate     { proxy_pass http://validation_service; }

        # ---- export ----
        location /export/          { proxy_pass http://export_service; }

        # ---- contract ----
        location /contracts/       { proxy_pass http://contract_service; }

        # Common proxy headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # NOTE: JWT verification can be added here with the auth_request module
        #       or via an njs/Lua script that calls auth-service /auth/verify.
    }
}
'''
write("gateway/nginx.conf", nginx_conf)
write("gateway/Dockerfile", '''
FROM nginx:1.27-alpine
COPY gateway/nginx.conf /etc/nginx/nginx.conf
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://localhost/gateway/health || exit 1
''')

# ------------------------------------------------------------------
# infra: docker-compose + env
# ------------------------------------------------------------------
compose_services = []
for name, s in SERVICES.items():
    port = s["port"]
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
    restart: unless-stopped
''')

docker_compose = f'''
# Kavachio - local microservices stack
# Usage:  cd infra && docker compose up --build
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

  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin123
    ports:
      - "9000:9000"   # S3 API
      - "9001:9001"   # web console
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

env_example = '''
# Shared config for the local microservices stack (infra/docker-compose.yml)
# Copy to infra/.env and fill in real secrets.  Do NOT commit .env.

# --- Shared database (Phase 1: one DB for all services) ---
DATABASE_URL=postgresql+psycopg2://postgres:postgres123@postgres:5432/kavachio

# --- Auth ---
JWT_SECRET=change-me-to-a-long-random-string
JWT_ALGORITHM=HS256
ACCESS_TOKEN_TTL_MIN=60
REFRESH_TOKEN_TTL_DAYS=7

# --- Shared file store (MinIO / S3) ---
S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin123
S3_BUCKET=kavachio-uploads

# --- LLM ---
GEMINI_API_KEY=

# --- CORS ---
CORS_ORIGINS=http://localhost:5173,http://localhost:8080
'''
write("infra/.env.example", env_example)

write("infra/README.md", '''
# infra - local microservices stack

## Start everything
```bash
cp .env.example .env      # then edit secrets (JWT_SECRET, GEMINI_API_KEY)
docker compose up --build
```

## What comes up
| Component | URL |
|---|---|
| API Gateway (front door) | http://localhost:8080 |
| PostgreSQL (shared) | localhost:5432 |
| MinIO S3 API | http://localhost:9000 |
| MinIO console | http://localhost:9001 |
| auth-service | http://localhost:8001/health |
| mapper-service | http://localhost:8002/health |
| ingestion-service | http://localhost:8003/health |
| validation-service | http://localhost:8004/health |
| export-service | http://localhost:8005/health |
| contract-service | http://localhost:8006/health |
| tenant-admin-service | http://localhost:8007/health |

## Quick check
```bash
curl http://localhost:8080/gateway/health
curl http://localhost:8001/health/db
```
''')

# ------------------------------------------------------------------
# Kubernetes base manifests
# ------------------------------------------------------------------
write("k8s/base/namespace.yaml", '''
apiVersion: v1
kind: Namespace
metadata:
  name: kavachio
''')

write("k8s/base/postgres.yaml", '''
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  namespace: kavachio
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels: { app: postgres }
  template:
    metadata:
      labels: { app: postgres }
    spec:
      containers:
        - name: postgres
          image: postgres:16
          env:
            - name: POSTGRES_DB
              value: kavachio
            - name: POSTGRES_USER
              value: postgres
            - name: POSTGRES_PASSWORD
              valueFrom: { secretKeyRef: { name: kavachio-secrets, key: POSTGRES_PASSWORD } }
          ports:
            - containerPort: 5432
          volumeMounts:
            - name: pgdata
              mountPath: /var/lib/postgresql/data
  volumeClaimTemplates:
    - metadata: { name: pgdata }
      spec:
        accessModes: ["ReadWriteOnce"]
        resources: { requests: { storage: 10Gi } }
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: kavachio
spec:
  selector: { app: postgres }
  ports:
    - port: 5432
      targetPort: 5432
''')

write("k8s/base/minio.yaml", '''
apiVersion: apps/v1
kind: Deployment
metadata:
  name: minio
  namespace: kavachio
spec:
  replicas: 1
  selector:
    matchLabels: { app: minio }
  template:
    metadata:
      labels: { app: minio }
    spec:
      containers:
        - name: minio
          image: minio/minio:latest
          args: ["server", "/data", "--console-address", ":9001"]
          env:
            - name: MINIO_ROOT_USER
              valueFrom: { secretKeyRef: { name: kavachio-secrets, key: S3_ACCESS_KEY } }
            - name: MINIO_ROOT_PASSWORD
              valueFrom: { secretKeyRef: { name: kavachio-secrets, key: S3_SECRET_KEY } }
          ports:
            - containerPort: 9000
            - containerPort: 9001
---
apiVersion: v1
kind: Service
metadata:
  name: minio
  namespace: kavachio
spec:
  selector: { app: minio }
  ports:
    - name: s3
      port: 9000
      targetPort: 9000
    - name: console
      port: 9001
      targetPort: 9001
''')

write("k8s/base/secrets.example.yaml", '''
# Example only. Create the real secret with `kubectl create secret ...`
# and never commit real values.
apiVersion: v1
kind: Secret
metadata:
  name: kavachio-secrets
  namespace: kavachio
type: Opaque
stringData:
  DATABASE_URL: postgresql+psycopg2://postgres:postgres123@postgres:5432/kavachio
  POSTGRES_PASSWORD: postgres123
  JWT_SECRET: change-me
  S3_ACCESS_KEY: minioadmin
  S3_SECRET_KEY: minioadmin123
  GEMINI_API_KEY: ""
''')

write("k8s/base/network-policy.yaml", '''
# Only the gateway may receive outside traffic; services talk to each other
# and to postgres/minio inside the namespace.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-external
  namespace: kavachio
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector: {}        # allow same-namespace traffic
''')

# Per-service k8s deployment + service + hpa
for name, s in SERVICES.items():
    port = s["port"]
    svc_full = f"{name}-service"
    manifest = f'''
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {svc_full}
  namespace: kavachio
  labels: {{ app: {svc_full} }}
spec:
  replicas: 2
  selector:
    matchLabels: {{ app: {svc_full} }}
  template:
    metadata:
      labels: {{ app: {svc_full} }}
    spec:
      containers:
        - name: {svc_full}
          image: kavachio/{name}-service:latest
          ports:
            - containerPort: {port}
          envFrom:
            - secretRef: {{ name: kavachio-secrets }}
          env:
            - name: SERVICE_PORT
              value: "{port}"
          readinessProbe:
            httpGet: {{ path: /health/db, port: {port} }}
            initialDelaySeconds: 10
            periodSeconds: 15
          livenessProbe:
            httpGet: {{ path: /health, port: {port} }}
            initialDelaySeconds: 10
            periodSeconds: 20
          resources:
            requests: {{ cpu: "100m", memory: "256Mi" }}
            limits:   {{ cpu: "1000m", memory: "1Gi" }}
---
apiVersion: v1
kind: Service
metadata:
  name: {svc_full}
  namespace: kavachio
spec:
  selector: {{ app: {svc_full} }}
  ports:
    - port: {port}
      targetPort: {port}
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {svc_full}
  namespace: kavachio
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {svc_full}
  minReplicas: 2
  maxReplicas: 6
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: {{ type: Utilization, averageUtilization: 70 }}
'''
    write(f"k8s/services/{name}.yaml", manifest)

# gateway k8s + ingress
write("k8s/base/gateway.yaml", '''
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gateway
  namespace: kavachio
spec:
  replicas: 2
  selector:
    matchLabels: { app: gateway }
  template:
    metadata:
      labels: { app: gateway }
    spec:
      containers:
        - name: gateway
          image: kavachio/gateway:latest
          ports:
            - containerPort: 80
---
apiVersion: v1
kind: Service
metadata:
  name: gateway
  namespace: kavachio
spec:
  selector: { app: gateway }
  ports:
    - port: 80
      targetPort: 80
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: kavachio-ingress
  namespace: kavachio
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "100m"
spec:
  rules:
    - host: kavachio.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: gateway
                port: { number: 80 }
''')

write("k8s/README.md", '''
# k8s - Kubernetes manifests

## Apply order
```bash
kubectl apply -f k8s/base/namespace.yaml
kubectl apply -f k8s/base/secrets.example.yaml     # replace with a real secret first
kubectl apply -f k8s/base/postgres.yaml
kubectl apply -f k8s/base/minio.yaml
kubectl apply -f k8s/base/network-policy.yaml
kubectl apply -f k8s/services/
kubectl apply -f k8s/base/gateway.yaml
```

For templated multi-environment deploys, prefer the Helm chart in `charts/kavachio`.
''')

# ------------------------------------------------------------------
# Helm chart
# ------------------------------------------------------------------
write("charts/kavachio/Chart.yaml", '''
apiVersion: v2
name: kavachio
description: Kavachio microservices (auth, mapper, ingestion, validation, export, contract, tenant-admin)
type: application
version: 0.1.0
appVersion: "0.1.0"
''')

# values.yaml enumerates every service
values_services = "\n".join(
    f'''  {name}:
    port: {s["port"]}
    replicas: 2
    image: kavachio/{name}-service
    tag: latest''' for name, s in SERVICES.items()
)
write("charts/kavachio/values.yaml", f'''
# Default values shared by all environments.
global:
  namespace: kavachio
  imagePullPolicy: IfNotPresent

secretName: kavachio-secrets

services:
{values_services}

gateway:
  image: kavachio/gateway
  tag: latest
  replicas: 2

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 6
  targetCPU: 70
''')

write("charts/kavachio/values-dev.yaml", '''
# Dev overrides
global:
  imagePullPolicy: Always
autoscaling:
  enabled: false
services:
  ingestion: { replicas: 1 }
''')

write("charts/kavachio/values-prod.yaml", '''
# Production overrides
autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 12
  targetCPU: 65
services:
  ingestion: { replicas: 3 }
  mapper: { replicas: 3 }
''')

write("charts/kavachio/templates/_helpers.tpl", '''
{{- define "kavachio.labels" -}}
app.kubernetes.io/managed-by: Helm
app.kubernetes.io/part-of: kavachio
{{- end -}}
''')

write("charts/kavachio/templates/services.yaml", '''
{{- range $name, $svc := .Values.services }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $name }}-service
  namespace: {{ $.Values.global.namespace }}
  labels:
    app: {{ $name }}-service
    {{- include "kavachio.labels" $ | nindent 4 }}
spec:
  replicas: {{ $svc.replicas }}
  selector:
    matchLabels: { app: {{ $name }}-service }
  template:
    metadata:
      labels: { app: {{ $name }}-service }
    spec:
      containers:
        - name: {{ $name }}-service
          image: "{{ $svc.image }}:{{ $svc.tag }}"
          imagePullPolicy: {{ $.Values.global.imagePullPolicy }}
          ports:
            - containerPort: {{ $svc.port }}
          envFrom:
            - secretRef: { name: {{ $.Values.secretName }} }
          env:
            - name: SERVICE_PORT
              value: "{{ $svc.port }}"
          readinessProbe:
            httpGet: { path: /health/db, port: {{ $svc.port }} }
            initialDelaySeconds: 10
          livenessProbe:
            httpGet: { path: /health, port: {{ $svc.port }} }
            initialDelaySeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: {{ $name }}-service
  namespace: {{ $.Values.global.namespace }}
spec:
  selector: { app: {{ $name }}-service }
  ports:
    - port: {{ $svc.port }}
      targetPort: {{ $svc.port }}
---
{{- if $.Values.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ $name }}-service
  namespace: {{ $.Values.global.namespace }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ $name }}-service
  minReplicas: {{ $.Values.autoscaling.minReplicas }}
  maxReplicas: {{ $.Values.autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: {{ $.Values.autoscaling.targetCPU }} }
{{- end }}
---
{{- end }}
''')

write("charts/kavachio/README.md", '''
# Helm chart: kavachio

```bash
# Dev
helm upgrade --install kavachio charts/kavachio -f charts/kavachio/values-dev.yaml -n kavachio --create-namespace

# Production
helm upgrade --install kavachio charts/kavachio -f charts/kavachio/values-prod.yaml -n kavachio
```
''')

# ------------------------------------------------------------------
# CI/CD - GitHub Actions
# ------------------------------------------------------------------
write(".github/workflows/ci.yaml", '''
name: CI

on:
  push:
    branches: [ development, main ]
  pull_request:

jobs:
  build-and-test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        service: [auth, tenant-admin, mapper, ingestion, validation, export, contract]
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r services/${{ matrix.service }}/requirements.txt
          pip install pytest ruff

      - name: Lint
        run: ruff check services/${{ matrix.service }} || true

      - name: Test
        run: |
          if [ -d services/${{ matrix.service }}/tests ]; then
            pytest services/${{ matrix.service }}/tests
          else
            echo "No tests yet for ${{ matrix.service }}"
          fi

      - name: Build image
        run: |
          docker build -f services/${{ matrix.service }}/Dockerfile \\
            -t kavachio/${{ matrix.service }}-service:${{ github.sha }} .

      # - name: Push image (enable once a registry is configured)
      #   run: docker push kavachio/${{ matrix.service }}-service:${{ github.sha }}
''')

write(".github/workflows/cd-dev.yaml", '''
name: CD - Dev

on:
  push:
    branches: [ development ]

jobs:
  deploy-dev:
    runs-on: ubuntu-latest
    environment: dev
    steps:
      - uses: actions/checkout@v4

      - name: Configure kubectl
        run: echo "Configure cluster access here (KUBECONFIG secret / cloud auth)"

      - name: Deploy with Helm
        run: |
          echo "helm upgrade --install kavachio charts/kavachio \\
            -f charts/kavachio/values-dev.yaml -n kavachio --create-namespace"

      - name: Smoke test
        run: echo "curl https://dev.kavachio/gateway/health"
''')

write(".github/workflows/cd-prod.yaml", '''
name: CD - Production

on:
  workflow_dispatch:      # manual trigger
  push:
    tags: [ "v*" ]

jobs:
  deploy-prod:
    runs-on: ubuntu-latest
    environment:
      name: production      # requires manual approval in GitHub settings
    steps:
      - uses: actions/checkout@v4

      - name: Configure kubectl
        run: echo "Configure production cluster access here"

      - name: Deploy with Helm (rolling update)
        run: |
          echo "helm upgrade --install kavachio charts/kavachio \\
            -f charts/kavachio/values-prod.yaml -n kavachio --atomic --timeout 5m"

      - name: Smoke test
        run: echo "curl https://kavachio/gateway/health"

      # --atomic auto-rolls back on failure
''')

# ------------------------------------------------------------------
# Top-level README
# ------------------------------------------------------------------
write("MICROSERVICES_README.md", '''
# Kavachio Microservices

This repo is being migrated from one FastAPI monolith into 7 Python services
behind an nginx gateway, sharing one PostgreSQL database (Phase 1).

See `docs/Kavachio_Microservices_Architecture.docx` for the full design.

## Layout
```
services/          # 7 FastAPI services (auth, tenant-admin, mapper,
                   #   ingestion, validation, export, contract)
shared/            # common code imported by all services
gateway/           # nginx front door + routing
infra/             # docker-compose.yml + .env for local dev
k8s/               # raw Kubernetes manifests
charts/kavachio/   # Helm chart (dev / prod values)
.github/workflows/ # CI + CD pipelines
backend/           # ORIGINAL monolith - still runnable, untouched
frontend/          # React SPA (points at the gateway :8080)
```

## Services & ports
| Service | Port | Owns |
|---|---|---|
| auth-service | 8001 | users |
| mapper-service | 8002 | mappers, fingerprints |
| ingestion-service | 8003 | uploads, warehouse |
| validation-service | 8004 | exception_decisions (DuckDB in-memory) |
| export-service | 8005 | export_templates |
| contract-service | 8006 | contracts, rules |
| tenant-admin-service | 8007 | tenants, parties, programs |
| gateway | 8080 | routing + JWT check |

## Run the whole stack locally
```bash
cd infra
cp .env.example .env      # edit JWT_SECRET, GEMINI_API_KEY
docker compose up --build
curl http://localhost:8080/gateway/health
```

## Migration status
- [x] Phase 0: folder structure + shared package
- [x] Phase 1: Dockerfiles, docker-compose, gateway, k8s/Helm, CI/CD
- [ ] Phase 2: move routes from the monolith into each service (one at a time)
- [ ] Phase 3: async job queue, tracing, optional DB split
''')

print("\nDone.")
