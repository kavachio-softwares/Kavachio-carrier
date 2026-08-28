"""
Compute each service's transitive shared/lib dependency set (static, catches lazy
imports since it scans all import statements textually). Output drives per-service
Dockerfiles that COPY only the shared modules a service needs -> editing a shared
module rebuilds only the services that actually use it.
"""
import os
import re
import json

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"
LIB = os.path.join(ROOT, "shared/lib")

SERVICES = ["auth", "tenant-admin", "mapper", "ingestion", "validation", "export", "contract"]

# Discover shared-lib module names (top-level .py + the package dir).
LIB_MODULES = set()
for e in os.listdir(LIB):
    if e.endswith(".py") and e != "__init__.py":
        LIB_MODULES.add(e[:-3])
    elif os.path.isdir(os.path.join(LIB, e)) and not e.startswith("__"):
        LIB_MODULES.add(e)   # package, e.g. contract_upload_services

# Platform modules every service always needs (bootstrap + framework glue).
ALWAYS = {"__init__", "db", "settings", "observability", "jobs", "clients",
          "auth_deps", "auth_tokens", "auth_utils"}

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([a-zA-Z_][\w]*)", re.M)


def imports_in(path):
    try:
        txt = open(path).read()
    except (IsADirectoryError, FileNotFoundError):
        return set()
    return {m for m in IMPORT_RE.findall(txt) if m in LIB_MODULES}


def module_files(mod):
    """Files that make up a shared-lib module (a .py, or a package dir tree)."""
    p = os.path.join(LIB, mod + ".py")
    if os.path.isfile(p):
        return [p]
    d = os.path.join(LIB, mod)
    if os.path.isdir(d):
        out = []
        for root, _, files in os.walk(d):
            out += [os.path.join(root, f) for f in files if f.endswith(".py")]
        return out
    return []


def closure(seed):
    seen = set(seed)
    stack = list(seed)
    while stack:
        mod = stack.pop()
        for f in module_files(mod):
            for dep in imports_in(f):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
    return seen


result = {}
for svc in SERVICES:
    app = os.path.join(ROOT, "services", svc, "app")
    seed = set()
    for f in os.listdir(app):
        if f.endswith(".py"):
            seed |= imports_in(os.path.join(app, f))
    dep = closure(seed) | ALWAYS
    result[svc] = sorted(dep)

for svc in SERVICES:
    print(f"{svc:14s} ({len(result[svc])}): {result[svc]}")

# reverse map: which services depend on each module (for impact awareness)
print("\n--- rebuild impact: editing <module> rebuilds these services ---")
rev = {}
for svc, mods in result.items():
    for m in mods:
        rev.setdefault(m, []).append(svc)
for m in sorted(rev):
    if m not in ALWAYS:
        print(f"  {m:26s} -> {rev[m]}")

with open(os.path.join(ROOT, "scripts/_service_deps.json"), "w") as f:
    json.dump(result, f, indent=2)
print("\nwrote scripts/_service_deps.json")
