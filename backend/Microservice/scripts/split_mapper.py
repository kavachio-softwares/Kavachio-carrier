"""
Split shared/lib/mapper.py into:
  - shared/lib/mapping_utils.py  : pure, stateless helpers (Excel IO, qualify,
                                   signatures, glom spec application, header cache).
                                   Stays shared - legitimately used by many services.
  - services/mapper/app/mapper.py: the LLM mapping engine (generate_mapping_multi +
                                   its Gemini helpers). Owned by mapper-service only;
                                   other services reach it via HTTP.

The LLM code depends on the utils (one-directional), so mapper.py just does
`from mapping_utils import *`.
"""
import ast
import os

ROOT = "/Users/at-mac11/Documents/Dinesh/POC/Kawachu/Code/Git/kavachio/backend/Microservice"
SRC = os.path.join(ROOT, "shared/lib/mapper.py")

# Names that are the LLM engine (everything else is a pure util).
LLM_NAMES = {
    "_build_candidates_prompt", "_is_acceptable_canonical",
    "_parse_candidates_response", "_gemini_candidates_call", "_call_llm_candidates",
    "_derive_llm_mapping", "_bucketize", "generate_mapping_multi",
    "_MAX_OUTPUT_TOKENS", "_FALLBACK_BATCH", "TOP_N_CANDIDATES", "SUCCESS_THRESHOLD",
    "LIKELY_THRESHOLD",
}

code = open(SRC).read()
lines = code.split("\n")
tree = ast.parse(code)


def span(node):
    start = node.lineno
    for d in getattr(node, "decorator_list", []):
        start = min(start, d.lineno)
    return "\n".join(lines[start - 1:node.end_lineno])


def names_of(node):
    out = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.append(node.name)
    elif isinstance(node, ast.Assign):
        out += [t.id for t in node.targets if isinstance(t, ast.Name)]
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        out.append(node.target.id)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        out += [a.asname or a.name.split(".")[0] for a in node.names]
    return out


future, util_parts, llm_parts, util_names = [], [], [], set()

for node in tree.body:
    if isinstance(node, ast.ImportFrom) and node.module == "__future__":
        future.append(span(node))
        continue
    nm = names_of(node)
    is_llm = any(n in LLM_NAMES for n in nm) if nm else False
    if is_llm:
        llm_parts.append(span(node))
    else:
        util_parts.append(span(node))
        for n in nm:
            util_names.add(n)

# ---- write mapping_utils.py (pure) ----
all_decl = "__all__ = [\n" + "".join(f"    {n!r},\n" for n in sorted(util_names)) + "]\n\n"
utils_code = (
    '"""Pure, stateless mapping utilities (Excel IO, qualify, signatures, glom spec\n'
    'application, header cache). Shared library - safe for any service to import."""\n'
    + ("\n".join(future) + "\n\n" if future else "")
    + all_decl
    + "\n\n".join(util_parts)
    + "\n"
)
open(os.path.join(ROOT, "shared/lib/mapping_utils.py"), "w").write(utils_code)
print(f"  + shared/lib/mapping_utils.py  ({len(util_names)} names)")

# ---- write services/mapper/app/mapper.py (LLM) ----
llm_code = (
    '"""LLM mapping engine (generate_mapping_multi + Gemini helpers). Owned by\n'
    'mapper-service. Pure utils come from the shared mapping_utils module."""\n'
    + ("\n".join(future) + "\n\n" if future else "")
    + "from mapping_utils import *  # noqa: F401,F403  (pure helpers + constants)\n"
    + "import mapping_utils as _mu  # ensure every name (incl. underscored) is available\n"
    + "globals().update({k: v for k, v in vars(_mu).items() if not k.startswith('__')})\n\n"
    + "\n\n".join(llm_parts)
    + "\n"
)
open(os.path.join(ROOT, "services/mapper/app/mapper.py"), "w").write(llm_code)
print(f"  + services/mapper/app/mapper.py  ({len(llm_parts)} LLM defs)")

# remove the old combined module from shared/lib (mapper-service owns the LLM half now)
os.remove(SRC)
print("  - shared/lib/mapper.py (removed; split into the two above)")
