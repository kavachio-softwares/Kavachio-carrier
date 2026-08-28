"""
Strict decoupling: copy each service's dependency subset from shared/lib INTO
services/<svc>/app/ so every service physically contains all its own code.
No shared/lib on the runtime path. Shared modules are DUPLICATED per service.

Because every module uses bare-name imports (`from db import ...`), copying the
files into app/ (which is on PYTHONPATH) makes them resolve locally - no import
rewriting needed.
"""
import json
import os
import shutil

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"
LIB = os.path.join(ROOT, "shared/lib")
PORTS = {"auth": 8001, "tenant-admin": 8007, "mapper": 8002, "ingestion": 8003,
         "validation": 8004, "export": 8005, "contract": 8006}

deps = json.load(open(os.path.join(ROOT, "scripts/_service_deps.json")))

print("Copying each service's real code into its own folder...\n")
for svc in PORTS:
    app = os.path.join(ROOT, "services", svc, "app")
    n_files = n_dirs = 0
    for m in deps[svc]:
        if m == "__init__":
            continue
        src_py = os.path.join(LIB, m + ".py")
        src_dir = os.path.join(LIB, m)
        if os.path.isfile(src_py):
            shutil.copy(src_py, os.path.join(app, m + ".py"))
            n_files += 1
        elif os.path.isdir(src_dir):
            dst = os.path.join(app, m)
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src_dir, dst,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            n_dirs += 1
    print(f"  {svc:14s} <- {n_files} modules + {n_dirs} package(s) copied into app/")

# ---- simplify each main.py bootstrap: app dir only, no shared/lib search ----
BOOT_OLD_START = "def _bootstrap_paths():"
for svc, port in PORTS.items():
    mp = os.path.join(ROOT, "services", svc, "app", "main.py")
    s = open(mp).read()
    # Replace the whole _bootstrap_paths function body with an app-dir-only version.
    start = s.index("def _bootstrap_paths():")
    end = s.index("_bootstrap_paths()")
    new_boot = (
        "def _bootstrap_paths():\n"
        "    # Self-contained: every module this service needs lives beside this file.\n"
        "    app_dir = os.path.dirname(os.path.abspath(__file__))\n"
        "    if app_dir not in sys.path:\n"
        "        sys.path.insert(0, app_dir)\n\n\n"
    )
    s = s[:start] + new_boot + s[end:]
    open(mp, "w").write(s)

# ---- regenerate self-contained Dockerfiles (no shared/lib) ----
for svc, port in PORTS.items():
    df = f'''FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gcc libpq-dev \\
 && rm -rf /var/lib/apt/lists/*

COPY services/{svc}/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Self-contained: the service's app/ holds ALL its code (routes + its own copies
# of every module it uses). Nothing shared at runtime -> editing this service
# rebuilds only this image.
COPY services/{svc}/app ./app

ENV PYTHONPATH=/app:/app/app
ENV SERVICE_PORT={port}
EXPOSE {port}

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:{port}/health').status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port {port}"]
'''
    open(os.path.join(ROOT, "services", svc, "Dockerfile"), "w").write(df)

print("\nmain.py bootstraps simplified + Dockerfiles regenerated (self-contained).")
