# Kavachio Microservices

7 independent Python services behind an nginx gateway, sharing one PostgreSQL
database and a MinIO/S3 file store, with a Redis job queue and request tracing.

> **Location:** everything for the microservices lives under `backend/Microservice/`
> (this folder): `services/`, `shared/`, `gateway/`, `infra/`, `k8s/`, `charts/`, `scripts/`.
> The original monolith stays at `backend/python-services/`. Product docs (design docx,
> architecture diagrams) stay at the repo-root `docs/`. CI/CD workflows stay at the
> repo-root `.github/workflows/` and reference paths as `backend/Microservice/...`.

Full design doc: repo-root `docs/Kavachio_Microservices_Architecture.docx`;
diagrams `docs/architecture_diagram.png` and `docs/api_routing_table.png`.

## Architecture — balanced: one shared library, slim services

Code is organised into three tiers to minimise **both** duplication and coupling:

- **`shared/lib/` (one copy, 25 modules + `contract_upload_services/`)** — the platform
  (DB models, canonical schema, auth, settings, observability, jobs, HTTP client) plus the
  shared engines/helpers used by 2+ services (`exporter`, `ingester`, `direct_*`,
  `mapping_utils`, `assembler`, `fingerprint`, `scd2_sql`, the `common_*` route helpers).
- **`services/<name>/app/`** — only what is unique to that service: `routes.py`, `main.py`,
  and its **single-owner engine** (`mapper.py` in mapper, `duckdb_validation.py` in
  validation, `email_utils.py` in auth). Just ~3–4 files each.
- **Two HTTP boundaries** — `mapper.py` and `duckdb_validation.py` live in exactly one
  service; others call them via `clients.py` (`/internal/*` endpoints).

Duplication dropped from **161 per-service files → 24** (the shared code exists once).

> **Deploy isolation without duplication:** each service's Dockerfile copies only the
> `shared/lib` modules it actually uses (`scripts/compute_deps.py`, every subset verified
> by importing with only that subset present). CI (`scripts/_service_deps.json`) rebuilds
> only the services whose changed files affect them.

### What a change rebuilds
| You edit… | Rebuilds |
|---|---|
| `services/<svc>/app/**` (routes, its own engine) | that service only |
| `services/mapper/app/mapper.py`, `services/validation/app/duckdb_validation.py` | that one |
| `shared/lib/email_utils.py` | auth only |
| `shared/lib/common_direct_routes.py`, `direct_render.py`, `direct_mapper.py` | ingestion, export |
| `shared/lib/common_main.py`, `mapping_utils.py`, `fingerprint.py` | mapper, ingestion, validation, export |
| `shared/lib/db.py`, `canonical.py`, `settings.py`, `auth_*` (platform) | all 7 (correct) |
| `shared/lib/exporter.py`, `ingester.py`, `contract_upload_services/` | all 7¹ |

¹ These are woven into the shared fabric (`exporter` is called from `scd2_sql`,
`direct_render`, `mapping_utils`; `ingester._ensure_tenant` runs inside tenant-admin's DB
transaction; `contract_upload_services` hosts the shared `gemini_service` AI gateway). They
are genuinely shared. Trimming their rebuild scope further would need the god-module
`common_app_routes` refactored + the export/ingestion flows under integration test.

### Regenerate after moving code
```bash
python scripts/compute_deps.py && python scripts/gen_dockerfiles.py   # recompute + re-verify subsets
```

## Services, ports & route ownership

| Service | Port | Routes | Owns (URL prefixes) |
|---|---|---|---|
| auth-service | 8001 | 12 | `/auth/*`, `/users*` |
| tenant-admin-service | 8007 | 20 | `/tenants*`, `/parties*`, `/programs*`, `/onboarding*`, `/dashboard*`, `/activity` |
| mapper-service | 8002 | 17 | `/mapper*`, `/data-model`, `/extra-fields*`, `/bdx/sheets`, `/bdx/preview`, `/api/canonical*` |
| ingestion-service | 8003 | 23 | `/bdx/upload`, `/uploads*`, `/dwh`, `/direct*`, `/admin/mapping-tasks*` |
| validation-service | 8004 | 4 | `/api/validate*`, `/export/downloads/*/decide` (DuckDB in-memory) |
| export-service | 8005 | 18 | `/export*` |
| contract-service | 8006 | 7 | `/programs/*/contracts*` |
| gateway | 8080 | — | routing + request-id + JWT |

*Verified: 101 route handlers split across the 7 services; every service builds its
full FastAPI app and generates a valid OpenAPI schema from only its own `app/` folder
(no shared directory on the path).*

## Layout
```
shared/lib/                 # ONE copy of the platform + shared engines/helpers
    db.py canonical.py data_model.py settings.py auth_*.py extras.py   # platform
    observability.py jobs.py clients.py
    exporter.py ingester.py direct_lane.py direct_render.py direct_mapper.py
    mapping_utils.py assembler.py fingerprint.py scd2_sql.py
    common_main.py common_app_routes.py common_direct_routes.py common_validation_routes.py
    contract_upload_services/
services/<name>/
    Dockerfile              # copies its shared/lib subset + services/<name>/app
    requirements.txt
    app/
        main.py             # bootstraps paths, builds app, includes routes
        routes.py           # THIS service's endpoint handlers
        # + its single-owner engine only:
        #   mapper.py (mapper) | duckdb_validation.py (validation) | email_utils.py (auth)
gateway/            # nginx front door
infra/              # docker-compose.yml + .env
k8s/  charts/       # Kubernetes manifests + Helm chart
scripts/            # tooling: compute_deps, gen_dockerfiles, rebalance, split_*

# outside this folder:
<repo>/backend/python-services/   # ORIGINAL monolith - untouched, still runnable
<repo>/.github/workflows/         # CI (per-affected-service build) + CD
<repo>/docs/                      # design docx + architecture/API diagrams
```

## Run locally
```bash
cd infra && cp .env.example .env      # set DATABASE_URL, JWT_SECRET, GEMINI_API_KEY
docker compose up --build
curl http://localhost:8080/gateway/health
```

## Decoupling status — complete

- [x] **Physical route separation** — 101 handlers split into per-service `routes.py`.
- [x] **Fully self-contained services** — no shared runtime directory; each service's
      `app/` holds its own copy of every module it uses. Verified with `shared/lib`
      deleted: every service still builds + serves valid OpenAPI.
- [x] **Per-service CI** — a change under `services/<svc>/` rebuilds only `<svc>`.
- [x] **Two HTTP domain boundaries** so provider code lives in one place:
      - **ingestion → mapper** (`POST /internal/mapping/generate`) — `mapper.py` only in mapper-service.
      - **export/ingestion → validation** (`POST /internal/validate`, `/internal/label-exceptions`) — `duckdb_validation.py` only in validation-service.

### Note on duplication (the strict-decoupling trade-off)
Modules used by several services (`db.py`, `canonical.py`, `exporter.py`,
`contract_upload_services/`, …) are **copied** into each — every service is an
independent unit. A fix to such a module must be applied to each service that carries a
copy. Two engines are deliberately NOT duplicated because they sit behind HTTP
boundaries: the mapper LLM (`mapper.py`) and the DuckDB validator
(`duckdb_validation.py`) live in exactly one service each; others reach them over HTTP.

### Regenerate / re-verify
```bash
python scripts/compute_deps.py         # recompute each service's module set
python scripts/make_self_contained.py  # (needs a source tree) re-copy into services
```
