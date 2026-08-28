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
