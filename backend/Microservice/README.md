# Kavachio Backend — Microservices

This folder holds the **microservices version** of the Kavachio backend. It does the
same job as the old single program in `backend/python-services/`, but the work is
split into **7 small services** that can be built, deployed, and restarted one at a
time. A single front door (the **gateway**) sits in front of them so the website only
ever talks to one address.

> New here? Read this file top to bottom. It is written in plain English.

---

## 1. What is a microservice (in one line)

Instead of one big program that does everything, we run **several small programs**,
each responsible for one area (login, uploads, validation, and so on). If you change
one area, you rebuild and redeploy **only that one** — the other six keep running.

---

## 2. The 7 services and what each one does

| Service | Port | In plain English, it handles… |
|---|---|---|
| **auth** | 8001 | Login, logout, users, passwords, and access tokens. |
| **tenant-admin** | 8007 | Tenants, **trading partners (parties)**, programs, the dashboard. |
| **mapper** | 8002 | AI column mapping — matching a spreadsheet's columns to our data model. |
| **ingestion** | 8003 | File uploads, the "direct" data lane, warehouse loading, mapping tasks. |
| **validation** | 8004 | Running the rules (DuckDB) and producing exceptions. |
| **export** | 8005 | Output templates and generating the final Excel workbooks. |
| **contract** | 8006 | Contract upload, reading clauses with AI, and building rules from them. |
| **gateway** | 8080 | The **front door** (nginx). The website talks only to this. It forwards each request to the right service. |

There are also two shared helpers running alongside:

- **PostgreSQL** — the database. It is **external / managed** (we do not run it here;
  we just connect to it). Uploaded files are stored inside the database, so there is
  **no separate file store** (no MinIO/S3).
- **Redis** — holds the status of long background jobs. Optional for a single machine.

---

## 3. How a request flows

```
Browser (React app, :5173)
        │  every call goes to ONE address
        ▼
API Gateway  (nginx, :8080)   ── checks the URL, forwards it ──►  the owning service
        │                                                          (auth / mapper / …)
        ▼
   the 7 services  ──────────────►  PostgreSQL (external)   ◄── all data + files
        │
        └── a few services also call each other over HTTP for two heavy jobs:
              • ingestion → mapper      (AI column mapping)
              • export/ingestion → validation  (DuckDB rules)
```

A picture version can be regenerated any time with
`python scripts/make_diagrams.py` (writes PNGs to the repo `docs/` folder).

---

## 4. Folder layout

```
backend/Microservice/
├── services/            # the 7 services — each has app/main.py, app/routes.py, + its own engine
│   ├── auth/  tenant-admin/  mapper/  ingestion/  validation/  export/  contract/
├── shared/lib/          # ONE shared library used by all services
│                        #   (database models, auth, settings, the shared engines, etc.)
├── gateway/             # nginx front door (nginx.conf + Dockerfile)
├── infra/               # run it locally: docker-compose.yml + .env.example
├── k8s/                 # deploy to Kubernetes: plain YAML manifests
├── charts/              # deploy to Kubernetes: Helm chart (templated, per-environment)
├── scripts/             # build-time tools (made the split, generate diagrams/docs)
├── README.md            # this file
└── MICROSERVICES_README.md   # deeper architecture notes / design decisions
```

**Why is most code in `shared/lib`?** To avoid copying the same file into every service.
Each service still only *ships* the parts of `shared/lib` it actually uses (its
Dockerfile copies just that subset), so services stay independent to build and deploy.
Only two pieces of logic live in exactly one service and are called over HTTP by the
others: the AI mapper (in `mapper`) and the DuckDB validator (in `validation`).

---

## 5. How to run it on your machine (local)

You need **Docker Desktop** running.

### Step 1 — create the config file
```bash
cd backend/Microservice/infra
cp .env.example .env
```

### Step 2 — fill in `.env`
Open `infra/.env` and set:
- `DATABASE_URL` — the address of your PostgreSQL database (see note below on the DB user).
- `JWT_SECRET` — any long random text.
- `GEMINI_API_KEY` — your Google Gemini key (needed for the AI features).

### Step 3 — start everything
```bash
# normal: connect to your external/managed database
docker compose up --build

# OR, if you just want a throwaway local database too:
docker compose --profile localdb up --build
```

### Step 4 — check it is alive
```bash
curl http://localhost:8080/gateway/health     # -> "gateway ok"
curl http://localhost:8001/health             # -> auth-service ok
curl http://localhost:8001/health/db          # -> checks the database connection
```

> **Health endpoints live at `/health` on each service's own port** (8001–8007), not
> through the gateway. Through the gateway you use the real routes, e.g. `/auth/login`.

---

## 6. Connect the website (frontend)

The React app decides which backend to call using one setting. Point it at the gateway:

```bash
cd frontend
echo 'VITE_API_URL=http://localhost:8080' > .env.local
npm install
npm run dev        # opens http://localhost:5173
```

**Important:** the app reads this setting **only when it starts**. If you change
`.env.local`, stop the dev server (Ctrl-C) and run `npm run dev` again.

- `:8080` = these new microservices.
- `:8000` = the old single-program backend (`backend/python-services/`).

---

## 7. Important note about the database user (this bites people)

The database uses **Row-Level Security (RLS)** — a database feature that hides rows
that do not belong to your tenant.

- Connect as the **`postgres`** user → RLS is bypassed, you see everything. (This is
  what the old backend does, so it "just works".)
- Connect as the **`kavachio_app`** user → RLS is enforced. It only shows rows when
  the app sets the current tenant on each request (`KAVACHIO_RLS=on`).

If you connect as `kavachio_app` **without** `KAVACHIO_RLS=on`, every screen looks
**empty** even though the data is there. So either:
- keep `DATABASE_URL` using `postgres` (simplest), **or**
- set `KAVACHIO_RLS=on` **and** `KAVACHIO_APP_DB_URL=...kavachio_app...` together
  (this is the more secure setup for production).

---

## 8. Everyday commands

```bash
cd backend/Microservice/infra

docker compose logs -f gateway export-service   # watch logs
docker compose up -d --build mapper-service     # rebuild ONE service only
docker compose ps                               # see what is running
docker compose down                             # stop everything
docker compose down -v                          # stop + wipe the local throwaway DB
```

Rebuilding one service and seeing the others stay up is the whole point of the split.

---

## 9. Deploying to a real server (Kubernetes)

Two options — pick one:

- **`k8s/`** — plain, explicit YAML files. Good for reading and learning.
  ```bash
  kubectl apply -f k8s/base/namespace.yaml
  kubectl apply -f k8s/base/secrets.example.yaml   # replace with real secrets first
  kubectl apply -f k8s/base/postgres.yaml          # points at the EXTERNAL database
  kubectl apply -f k8s/base/network-policy.yaml
  kubectl apply -f k8s/services/
  kubectl apply -f k8s/base/gateway.yaml
  ```
- **`charts/kavachio`** — a Helm chart (templated). Better for multiple environments
  (`values-dev.yaml`, `values-prod.yaml`). The CI/CD workflows use this.

The database is external in both, so `postgres.yaml` is only an **alias** that points
the name `postgres` at your managed database — not an actual database server.

---

## 10. Automatic builds (CI/CD)

The GitHub Actions workflows live at the repo root in `.github/workflows/`:

- **ci.yaml** — when you change one service under `backend/Microservice/services/<name>/`,
  it rebuilds **only that service**. Changing `shared/` rebuilds all of them (correct,
  because they all share it).
- **cd-dev.yaml / cd-prod.yaml** — deploy using the Helm chart.

---

## 11. Timeouts (why long jobs used to fail)

Some jobs are slow (AI mapping, contract reading, big Excel files). Two limits matter,
both set to **30 minutes** so slow jobs are not cut off:

- **Gateway → service**: `proxy_read_timeout` in `gateway/nginx.conf` (default nginx is
  only 60s — that caused the old 504 errors).
- **Service → service**: `SERVICE_HTTP_TIMEOUT` in `.env` (default was only 120s).

A longer-term improvement is to run these jobs in the background (the `jobs.py` +
Redis machinery is already wired for it) so no request has to wait at all.

---

## 12. Quick troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Every screen is empty, but data exists | Connected as `kavachio_app` without RLS on | Use the `postgres` user, or turn on `KAVACHIO_RLS` (see §7). |
| Website shows old data / wrong server | Frontend pointing at `:8000` (old backend) | Set `VITE_API_URL=http://localhost:8080`, restart `npm run dev`. |
| A long build spins then the loader vanishes | Request timed out at the gateway | Timeouts are now 30 min (§11); rebuild the gateway if you changed them. |
| Gateway shows "unhealthy" but works | Health check used `localhost` (IPv6) | Already fixed to `127.0.0.1` in `gateway/Dockerfile`. |
| `/auth/health` returns "Not Found" | That is not a real route | Health is at `/health` on the service's own port (§5). |

---

## 13. The old backend is still here

`backend/python-services/` (the original single program) is untouched and still runs on
port **8000**. Nothing here deletes or changes it. You can run either one.
