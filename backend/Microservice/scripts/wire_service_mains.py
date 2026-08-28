"""Wire each service main.py to include its own routes.py, and repoint Dockerfiles at shared/lib."""
import os

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"
SERVICES = {"auth": 8001, "tenant-admin": 8007, "mapper": 8002, "ingestion": 8003,
            "validation": 8004, "export": 8005, "contract": 8006}


def write(rel, content):
    p = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(content.lstrip("\n"))
    print("  ~", rel)


for name, port in SERVICES.items():
    svc_full = f"{name}-service"
    main_py = f'''
"""
{svc_full} - owns its route handlers in app/routes.py.

Business/engine modules currently live in shared/lib (imported here). Cross-service
calls are being moved to HTTP (see shared/lib/clients.py); until a given boundary is
converted it still imports the shared engine directly.
"""
import os
import sys


def _bootstrap_paths():
    app_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, app_dir)                      # so `import routes` works
    here = app_dir
    for _ in range(6):
        cand = os.path.join(here, "shared", "lib")
        if os.path.isdir(cand):
            sys.path.insert(0, cand)
            return
        here = os.path.dirname(here)
    for cand in ("/app/shared/lib",):
        if os.path.isdir(cand):
            sys.path.insert(0, cand)
            return
    raise RuntimeError("could not locate shared/lib")


_bootstrap_paths()

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from db import init_db  # noqa: E402
init_db()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from observability import install_observability  # noqa: E402
from jobs import install_jobs_api  # noqa: E402
from routes import router  # noqa: E402

SERVICE_NAME = "{svc_full}"
SERVICE_PORT = {port}

app = FastAPI(title="Kavachio {svc_full}", version="0.2.0")
install_observability(app, SERVICE_NAME)
install_jobs_api(app)

_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health", tags=["_meta"])
def health():
    return {{"service": SERVICE_NAME, "status": "ok", "routes": len(router.routes)}}


@app.get("/health/db", tags=["_meta"])
def health_db():
    try:
        from sqlalchemy import text
        from db import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {{"service": SERVICE_NAME, "db": "ok"}}
    except Exception as exc:  # noqa: BLE001
        return {{"service": SERVICE_NAME, "db": "error", "detail": str(exc)}}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=SERVICE_PORT, reload=True)
'''
    write(f"services/{name}/app/main.py", main_py)

    dockerfile = f'''
FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gcc libpq-dev \\
 && rm -rf /var/lib/apt/lists/*

COPY services/{name}/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Shared library (platform + engines) FIRST, then this service's own code.
# Editing services/{name}/app/** busts only the last layer -> only this image rebuilds.
COPY shared/lib ./shared/lib
COPY services/{name}/app ./app

ENV PYTHONPATH=/app:/app/shared/lib
ENV SERVICE_PORT={port}
EXPOSE {port}

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:{port}/health').status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port {port}"]
'''
    write(f"services/{name}/Dockerfile", dockerfile)

print("\nDone.")
