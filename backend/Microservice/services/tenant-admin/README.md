# tenant-admin-service

Master data: tenants, parties, programs.

| Property | Value |
|---|---|
| Port | `8007` |
| Owns (writes) | `tenants, parties, programs` |
| Main paths | `/tenants, /parties, /programs` |
| Modules to migrate (Phase 2) | tenant/party/program routes from app_routes.py |

## Run locally (standalone)
```bash
cd services/tenant-admin
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://postgres:postgres123@localhost:5432/kavachio
uvicorn app.main:app --host 0.0.0.0 --port 8007 --reload
```

## Health
- `GET /health` - liveness
- `GET /health/db` - shared-DB readiness
