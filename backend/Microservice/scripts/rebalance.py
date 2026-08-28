"""
Rebalance: move the shared modules (Categories A + B) back into ONE shared/lib,
and slim each service to just its own code (routes.py, main.py, and its single-owner
engine). Keeps duplication near zero while preserving per-service Docker subsetting.
"""
import os
import shutil

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"
SVC = ["auth", "tenant-admin", "mapper", "ingestion", "validation", "export", "contract"]
PORTS = {"auth": 8001, "tenant-admin": 8007, "mapper": 8002, "ingestion": 8003,
         "validation": 8004, "export": 8005, "contract": 8006}

# Category A + B -> the single shared library.
SHARED = [
    # A - platform
    "db", "canonical", "data_model", "settings",
    "auth_deps", "auth_tokens", "auth_utils", "observability", "jobs", "clients", "extras",
    # B - shared engines / helpers
    "exporter", "ingester", "direct_lane", "direct_render", "direct_mapper",
    "mapping_utils", "assembler", "fingerprint", "scd2_sql",
    "common_main", "common_app_routes", "common_direct_routes", "common_validation_routes",
]
SHARED_PKG = "contract_upload_services"

# Category C - stays in exactly one service.
KEEP_IN_SERVICE = {"mapper.py", "duckdb_validation.py", "email_utils.py"}
# Category D - always per-service.
ALWAYS_KEEP = {"routes.py", "main.py", "__init__.py"}

lib = os.path.join(ROOT, "shared/lib")
os.makedirs(lib, exist_ok=True)
open(os.path.join(lib, "__init__.py"), "w").write(
    '"""Kavachio shared library: platform (db/auth/schema/config) + shared engines."""\n')

# 1) Reconstruct shared/lib by pulling each shared module from whichever service has it.
missing = []
for mod in SHARED:
    placed = False
    for svc in SVC:
        src = os.path.join(ROOT, "services", svc, "app", mod + ".py")
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(lib, mod + ".py"))
            placed = True
            break
    if not placed:
        missing.append(mod)
# shared package
for svc in SVC:
    src = os.path.join(ROOT, "services", svc, "app", SHARED_PKG)
    if os.path.isdir(src):
        dst = os.path.join(lib, SHARED_PKG)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        break

print(f"shared/lib rebuilt: {len(SHARED)} modules + {SHARED_PKG}/  (missing: {missing})")

# 2) Slim each service: delete anything that now lives in shared/lib.
shared_names = set(SHARED)
for svc in SVC:
    app = os.path.join(ROOT, "services", svc, "app")
    removed = 0
    for entry in os.listdir(app):
        full = os.path.join(app, entry)
        if entry in ALWAYS_KEEP or entry in KEEP_IN_SERVICE:
            continue
        if entry == "__pycache__":
            shutil.rmtree(full, ignore_errors=True)
            continue
        if entry == SHARED_PKG and os.path.isdir(full):
            shutil.rmtree(full)
            removed += 1
            continue
        if entry.endswith(".py") and entry[:-3] in shared_names:
            os.remove(full)
            removed += 1
    kept = sorted(f for f in os.listdir(app) if f.endswith(".py"))
    print(f"  {svc:14s} slimmed (removed {removed}); keeps: {kept}")

# 3) Restore each main.py bootstrap to find shared/lib (self dir + shared/lib).
TMPL = '''"""
{full} - owns its routes + its single-owner engine; imports the shared library.
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

SERVICE_NAME = "{full}"
SERVICE_PORT = {port}

app = FastAPI(title="Kavachio {full}", version="0.2.0")
install_observability(app, SERVICE_NAME)
install_jobs_api(app)
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins or ["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
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
for svc, port in PORTS.items():
    open(os.path.join(ROOT, "services", svc, "app", "main.py"), "w").write(
        TMPL.format(full=f"{svc}-service", port=port))
print("\nmain.py bootstraps restored (self dir + shared/lib).")
