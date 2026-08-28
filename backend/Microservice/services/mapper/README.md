# mapper-service

LLM-driven column mapping (Gemini + embeddings, cosine fallback).

| Property | Value |
|---|---|
| Port | `8002` |
| Owns (writes) | `mappers, fingerprints` |
| Main paths | `/mapper/*, /bdx/sheets, /bdx/preview, /data-model` |
| Modules to migrate (Phase 2) | mapper.py, fingerprint.py, data_model.py |

## Run locally (standalone)
```bash
cd services/mapper
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://postgres:postgres123@localhost:5432/kavachio
uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

## Health
- `GET /health` - liveness
- `GET /health/db` - shared-DB readiness
