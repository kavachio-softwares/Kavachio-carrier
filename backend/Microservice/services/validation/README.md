# validation-service

Rule checks using an in-memory DuckDB. 100% Python (no Node).

| Property | Value |
|---|---|
| Port | `8004` |
| Owns (writes) | `exception_decisions` |
| Main paths | `/api/validate, /api/validate/exceptions/decide` |
| Modules to migrate (Phase 2) | duckdb_validation.py, validation_routes.py |

## Run locally (standalone)
```bash
cd services/validation
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://postgres:postgres123@localhost:5432/kavachio
uvicorn app.main:app --host 0.0.0.0 --port 8004 --reload
```

## Health
- `GET /health` - liveness
- `GET /health/db` - shared-DB readiness
