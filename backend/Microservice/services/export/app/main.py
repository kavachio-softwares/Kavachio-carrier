"""
export-service - owns its routes + its single-owner engine; imports the shared library.
"""
import os
import sys


def _bootstrap_paths():
    app_dir = os.path.dirname(os.path.abspath(__file__))
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)                 # this service's own code
    here = app_dir
    for _ in range(6):
        cand = os.path.join(here, "shared", "lib")
        if os.path.isdir(cand):
            sys.path.insert(0, cand)                 # the shared library
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

SERVICE_NAME = "export-service"
SERVICE_PORT = 8005

app = FastAPI(title="Kavachio export-service", version="0.2.0")
install_observability(app, SERVICE_NAME)
install_jobs_api(app)
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins or ["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router)


@app.get("/health", tags=["_meta"])
def health():
    return {"service": SERVICE_NAME, "status": "ok", "routes": len(router.routes)}


@app.get("/health/db", tags=["_meta"])
def health_db():
    try:
        from sqlalchemy import text
        from db import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"service": SERVICE_NAME, "db": "ok"}
    except Exception as exc:  # noqa: BLE001
        return {"service": SERVICE_NAME, "db": "error", "detail": str(exc)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=SERVICE_PORT, reload=True)
