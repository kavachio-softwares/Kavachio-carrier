"""
AST route splitter (foundation for full decoupling).

Reads the 4 monolith route files and produces:
  - shared/lib/common_<file>.py   : every top-level helper/import/model/constant
                                     from that file, MINUS the endpoint functions,
                                     the app/router creation, and side-effect calls
                                     (init_db, load_dotenv, add_middleware, include_router).
  - services/<svc>/app/routes.py  : ONLY that service's endpoint functions, on a
                                     fresh APIRouter, importing the commons.

This physically moves each service's endpoint handlers into its own folder, so
editing them rebuilds only that service. Business/engine modules are handled
separately (moved into shared/lib for now, decoupled to HTTP in a later step).
"""
import ast
import os
import re

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"
SRC = os.path.join(ROOT, "backend/python-services")

SOURCES = ["main.py", "app_routes.py", "validation_routes.py", "direct_routes.py"]

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

# Route ownership - identical to shared/core/service_split.py (verified: 0 orphans).
PRIORITY = [
    ("contract", [r"^/programs/[^/]+/contracts"]),
    ("validation", [r"^/api/validate", r"^/export/downloads/[^/]+/decide$"]),
    ("export", [r"^/export"]),
    ("mapper", [r"^/mapper", r"^/data-model", r"^/bdx/sheets", r"^/bdx/preview",
                r"^/api/canonical", r"^/extra-fields"]),
    ("ingestion", [r"^/bdx/upload", r"^/uploads", r"^/dwh", r"^/direct",
                   r"^/admin/mapping-tasks"]),
    ("tenant-admin", [r"^/tenants", r"^/parties", r"^/programs", r"^/onboarding",
                      r"^/dashboard", r"^/activity"]),
    ("auth", [r"^/auth", r"^/users"]),
]
_COMPILED = [(svc, [re.compile(p) for p in pats]) for svc, pats in PRIORITY]


def owner_of(path):
    for svc, pats in _COMPILED:
        if any(p.match(path) for p in pats):
            return svc
    return None


def rewrite_cross_imports(code: str) -> str:
    """Point references at the moved-apart module names."""
    # main.py imports tenancy helpers + routers from app_routes; keep only helpers.
    code = code.replace(
        "from app_routes import router as app_router, resolve_tenant_id, assert_tenant_owns",
        "from common_app_routes import resolve_tenant_id, assert_tenant_owns",
    )
    code = re.sub(r"\bfrom app_routes import ", "from common_app_routes import ", code)
    code = re.sub(r"\bfrom validation_routes import ", "from common_validation_routes import ", code)
    code = re.sub(r"\bfrom direct_routes import ", "from common_direct_routes import ", code)
    # Lazy `from main import <helper>` inside endpoints -> the extracted common module.
    code = re.sub(r"\bfrom main import ", "from common_main import ", code)
    # Drop now-dangling router aliases if they slipped into an import list.
    code = code.replace("import router as app_router, ", "import ")
    code = code.replace("import validation_router, ", "import ")
    code = code.replace("import direct_router, ", "import ")
    # Remove standalone router-alias imports (only used for the removed include_router).
    code = re.sub(r"^[ \t]*from common_\w+ import router as \w+[ \t]*\n", "", code, flags=re.M)
    return code


def decorator_route(node):
    """If node is an endpoint function, return (path, methods, decorator_kind). Else None."""
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr in HTTP_METHODS:
            base = target.value
            if isinstance(base, ast.Name) and base.id in ("app", "router"):
                path = None
                if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                    path = dec.args[0].value
                return path, target.attr, base.id
    return None


def node_span(node, lines):
    """Return source text for a top-level node, including its decorators + leading comments."""
    start = node.lineno
    for dec in getattr(node, "decorator_list", []):
        start = min(start, dec.lineno)
    end = node.end_lineno
    return "\n".join(lines[start - 1:end])


def is_side_effect(node):
    """True for app/router creation and monolith bootstrap calls we must NOT copy into commons."""
    # assignments: app = FastAPI(...), router = APIRouter()
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in ("app", "router"):
                return True
    # bare calls: init_db(), load_dotenv(...), app.add_middleware(...), app.include_router(...)
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        f = node.value.func
        if isinstance(f, ast.Name) and f.id in ("init_db", "load_dotenv"):
            return True
        if isinstance(f, ast.Attribute) and f.attr in ("add_middleware", "include_router"):
            return True
    # if __name__ == "__main__": ...
    if isinstance(node, ast.If):
        t = node.test
        if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                and t.left.id == "__name__"):
            return True
    return False


def main():
    # svc -> list of (source_file, endpoint_code)
    svc_endpoints = {svc: [] for svc, _ in PRIORITY}
    commons = {}          # source_file -> common code string
    commons_future = {}   # source_file -> hoisted __future__ imports
    all_names = {}        # source_file -> set of top-level names (for __all__)

    for fname in SOURCES:
        path = os.path.join(SRC, fname)
        code = open(path).read()
        lines = code.split("\n")
        tree = ast.parse(code)

        common_parts = []
        future_parts = []
        names = set()

        for node in tree.body:
            # __future__ imports must be hoisted to the very top of the module.
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                future_parts.append(node_span(node, lines))
                for n in _top_names(node):
                    names.add(n)
                continue
            route = decorator_route(node) if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
            if route is not None:
                path_str, method, kind = route
                seg = node_span(node, lines)
                seg = seg.replace("@app.", "@router.")     # main.py uses @app
                owner = owner_of(path_str) if path_str else None
                if owner is None:
                    print(f"  !! ORPHAN endpoint {method.upper()} {path_str} in {fname}")
                    continue
                svc_endpoints[owner].append((fname, seg))
                continue

            if is_side_effect(node):
                continue

            # keep in common; record top-level names for __all__
            common_parts.append(node_span(node, lines))
            for n in _top_names(node):
                names.add(n)

        common_code = rewrite_cross_imports("\n\n".join(common_parts))
        commons[fname] = common_code
        commons_future[fname] = "\n".join(future_parts)
        all_names[fname] = names

    # ---- write commons ----
    lib = os.path.join(ROOT, "shared/lib")
    os.makedirs(lib, exist_ok=True)
    stem = {f: f"common_{f[:-3]}" for f in SOURCES}
    dropped_aliases = {"app_router", "validation_router", "direct_router", "router", "app"}
    for fname in SOURCES:
        names = sorted(all_names[fname] - dropped_aliases)
        header = (
            f'"""Shared helpers extracted from the monolith\'s {fname} '
            f'(endpoints removed)."""\n'
        )
        all_decl = "__all__ = [\n" + "".join(f"    {n!r},\n" for n in names) + "]\n\n"
        future = commons_future[fname]
        future_block = (future + "\n\n") if future else ""
        out = header + future_block + all_decl + commons[fname] + "\n"
        with open(os.path.join(lib, stem[fname] + ".py"), "w") as f:
            f.write(out)
        print(f"  + shared/lib/{stem[fname]}.py   ({len(names)} names)")

    # ---- write per-service routes.py ----
    svc_dir_name = {
        "auth": "auth", "tenant-admin": "tenant-admin", "mapper": "mapper",
        "ingestion": "ingestion", "validation": "validation", "export": "export",
        "contract": "contract",
    }
    for svc, eps in svc_endpoints.items():
        used_sources = sorted({fn for fn, _ in eps})
        imports = "\n".join(f"from {stem[fn]} import *" for fn in used_sources)
        # Also pull underscore-prefixed names explicitly (import * still gets them via __all__).
        body = "\n\n\n".join(seg for _, seg in eps)
        body = rewrite_cross_imports(body)
        header = (
            f'"""Route handlers owned by {svc}-service. Extracted from: '
            f'{", ".join(used_sources)}."""\n'
            "from __future__ import annotations\n"
            "from fastapi import APIRouter\n"
            f"{imports}\n\n"
            "router = APIRouter()\n\n\n"
        )
        d = os.path.join(ROOT, "services", svc_dir_name[svc], "app")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "routes.py"), "w") as f:
            f.write(header + body + "\n")
        print(f"  + services/{svc_dir_name[svc]}/app/routes.py   ({len(eps)} endpoints from {used_sources})")


def _top_names(node):
    """Top-level names a node introduces (for __all__)."""
    out = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.append(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                out.append(t.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        out.append(node.target.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            out.append(a.asname or a.name.split(".")[0])
    return out


if __name__ == "__main__":
    print("Splitting monolith routes...\n")
    main()
    print("\nDone.")
