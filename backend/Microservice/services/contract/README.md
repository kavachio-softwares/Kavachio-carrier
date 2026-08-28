# contract-service

Contract uploads: PDF/Word extraction, LLM rule generation, versioning.

| Property | Value |
|---|---|
| Port | `8006` |
| Owns (writes) | `contracts, contract rule tables` |
| Main paths | `/contracts/*` |
| Modules to migrate (Phase 2) | contract_upload_services/ (15+ modules) |

## Run locally (standalone)
```bash
cd services/contract
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://postgres:postgres123@localhost:5432/kavachio
uvicorn app.main:app --host 0.0.0.0 --port 8006 --reload
```

## Health
- `GET /health` - liveness
- `GET /health/db` - shared-DB readiness
