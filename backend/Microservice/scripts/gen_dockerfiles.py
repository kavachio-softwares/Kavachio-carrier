"""
Generate per-service Dockerfiles that COPY only the shared/lib modules the service
needs (from scripts/_service_deps.json), then VERIFY each subset is complete by
importing the service's routes with ONLY that subset on the path.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"
LIB = os.path.join(ROOT, "shared/lib")
PORTS = {"auth": 8001, "tenant-admin": 8007, "mapper": 8002, "ingestion": 8003,
         "validation": 8004, "export": 8005, "contract": 8006}

deps = json.load(open(os.path.join(ROOT, "scripts/_service_deps.json")))


def copy_spec(mods):
    """Return (files, dirs) COPY lines for a module list."""
    files, dirs = [], []
    for m in mods:
        if os.path.isfile(os.path.join(LIB, m + ".py")):
            files.append(f"shared/lib/{m}.py")
        elif os.path.isdir(os.path.join(LIB, m)):
            dirs.append(m)
    return files, dirs


def gen_dockerfile(svc):
    port = PORTS[svc]
    mods = deps[svc]
    files, dirs = copy_spec(mods)
    # chunk file COPY to keep lines readable
    file_copies = ""
    CH = 6
    for i in range(0, len(files), CH):
        chunk = " ".join(files[i:i + CH])
        file_copies += f"COPY {chunk} ./shared/lib/\n"
    dir_copies = "".join(
        f"COPY shared/lib/{d} ./shared/lib/{d}\n" for d in dirs)
    return f'''FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \\
    gcc libpq-dev \\
 && rm -rf /var/lib/apt/lists/*

COPY services/{svc}/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Only the shared/lib modules THIS service needs (computed by scripts/compute_deps.py).
# Editing a shared module rebuilds only the services whose subset includes it.
{file_copies}{dir_copies}COPY services/{svc}/app ./app

ENV PYTHONPATH=/app:/app/shared/lib
ENV SERVICE_PORT={port}
EXPOSE {port}

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \\
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:{port}/health').status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port {port}"]
'''


def verify_subset(svc):
    """Copy ONLY the service's subset to a temp 'shared/lib', put it (and the app
    dir) on the path, and import routes. Missing module -> ImportError -> fail."""
    mods = deps[svc]
    tmp = tempfile.mkdtemp(prefix=f"subset_{svc}_")
    tlib = os.path.join(tmp, "lib")
    os.makedirs(tlib)
    for m in mods:
        src_py = os.path.join(LIB, m + ".py")
        src_dir = os.path.join(LIB, m)
        if os.path.isfile(src_py):
            shutil.copy(src_py, tlib)
        elif os.path.isdir(src_dir):
            shutil.copytree(src_dir, os.path.join(tlib, m))
    code = (
        "import sys,os\n"
        f"sys.path.insert(0, {tlib!r})\n"                       # ONLY the subset
        f"sys.path.insert(0, {os.path.join(ROOT,'services',svc,'app')!r})\n"
        "import routes\n"
        "print('ROUTES_OK', len(routes.router.routes))\n"
    )
    env = dict(os.environ, JWT_SECRET="v", GEMINI_API_KEY="x", CORS_ORIGINS="*")
    env.pop("DATABASE_URL", None)
    _repo_root = os.path.dirname(os.path.dirname(ROOT))   # ROOT is <repo>/backend/Microservice
    r = subprocess.run([os.path.join(_repo_root, "backend/python-services/venv/bin/python"), "-c", code],
                       capture_output=True, text=True, env=env)
    shutil.rmtree(tmp, ignore_errors=True)
    ok = "ROUTES_OK" in r.stdout
    detail = "" if ok else (r.stderr.strip().splitlines() or [""])[-1]
    return ok, detail


print("Generating + verifying per-service Dockerfiles...\n")
allok = True
for svc in PORTS:
    with open(os.path.join(ROOT, "services", svc, "Dockerfile"), "w") as f:
        f.write(gen_dockerfile(svc))
    ok, detail = verify_subset(svc)
    allok &= ok
    print(f"  {'OK ' if ok else 'FAIL'} {svc:14s} subset={len(deps[svc])} modules  {detail}")

print("\n" + ("ALL SUBSETS COMPLETE + VERIFIED" if allok else "SOME SUBSETS INCOMPLETE"))
sys.exit(0 if allok else 1)
