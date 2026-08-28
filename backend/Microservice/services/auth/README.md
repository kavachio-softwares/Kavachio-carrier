# auth-service

Login, logout, token refresh, password reset, user identity.

| Property | Value |
|---|---|
| Port | `8001` |
| Owns (writes) | `users` |
| Main paths | `/auth/login, /auth/refresh, /auth/logout, /auth/reset` |
| Modules to migrate (Phase 2) | auth_deps.py, auth_tokens.py, auth_utils.py, email_utils.py |

## Run locally (standalone)
```bash
cd services/auth
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://postgres:postgres123@localhost:5432/kavachio
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

## Health
- `GET /health` - liveness
- `GET /health/db` - shared-DB readiness
