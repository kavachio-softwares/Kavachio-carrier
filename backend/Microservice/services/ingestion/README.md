# ingestion-service

Reads Excel, applies mapping spec (glom), writes to the canonical warehouse (SCD2).

| Property | Value |
|---|---|
| Port | `8003` |
| Owns (writes) | `uploads, uploads_policy, canonical warehouse tables` |
| Main paths | `/bdx/upload, /direct/*, /uploads/*` |
| Modules to migrate (Phase 2) | ingester.py, direct_lane.py, direct_mapper.py, direct_render.py, assembler.py, scd2_sql.py |

## Run locally (standalone)
```bash
cd services/ingestion
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://postgres:postgres123@localhost:5432/kavachio
uvicorn app.main:app --host 0.0.0.0 --port 8003 --reload
```

## Health
- `GET /health` - liveness
- `GET /health/db` - shared-DB readiness
