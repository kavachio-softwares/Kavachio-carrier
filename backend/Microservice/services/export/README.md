# export-service

Builds output Excel workbooks from approved templates + warehouse data.

| Property | Value |
|---|---|
| Port | `8005` |
| Owns (writes) | `export_templates` |
| Main paths | `/export/template/*, /export/generate` |
| Modules to migrate (Phase 2) | exporter.py, assembler.py |

## Run locally (standalone)
```bash
cd services/export
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://postgres:postgres123@localhost:5432/kavachio
uvicorn app.main:app --host 0.0.0.0 --port 8005 --reload
```

## Health
- `GET /health` - liveness
- `GET /health/db` - shared-DB readiness
