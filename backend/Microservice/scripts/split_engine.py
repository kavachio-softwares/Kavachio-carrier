"""
Generic call-graph engine splitter.

Given a module and a set of PURE public entry-point functions, compute the
transitive closure of functions they call, and split the module into:
  - <pure_out>   : the pure closure (functions + ALL module-level imports/consts).
                   Stays in shared/lib - safe for any service to import.
  - <domain_out> : everything else (the domain / LLM / heavy functions). Owned by
                   one service. Does `from <pure_mod> import *` for shared names.

A pure function must never call a domain function (verified: the closure is
computed FROM the pure entry-points, so anything a pure fn calls is pure by
construction; we then assert no domain name is referenced by a pure fn).

Usage: python split_engine.py <src.py> <pure_mod_name> <pure_out.py> <domain_out.py> <entry1> <entry2> ...
"""
import ast
import sys


def top_functions(tree):
    return {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def names_used(node):
    used = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            used.add(sub.id)
    return used


def span(node, lines):
    start = node.lineno
    for d in getattr(node, "decorator_list", []):
        start = min(start, d.lineno)
    return "\n".join(lines[start - 1:node.end_lineno])


def main():
    src_path, pure_mod, pure_out, domain_out = sys.argv[1:5]
    entries = set(sys.argv[5:])

    code = open(src_path).read()
    lines = code.split("\n")
    tree = ast.parse(code)
    funcs = top_functions(tree)

    # transitive closure of calls from the pure entry-points
    pure = set()
    stack = list(entries)
    while stack:
        f = stack.pop()
        if f in pure or f not in funcs:
            continue
        pure.add(f)
        for nm in names_used(funcs[f]):
            if nm in funcs and nm not in pure:
                stack.append(nm)

    domain = set(funcs) - pure

    # sanity: a pure fn must not reference a domain fn
    for pf in pure:
        bad = names_used(funcs[pf]) & domain
        if bad:
            print(f"  !! pure fn {pf} references domain fn(s) {bad} — not cleanly splittable")
            sys.exit(2)

    future, mod_level = [], []          # module-level (non-function) statements
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            future.append(span(node, lines))
        else:
            mod_level.append(span(node, lines))

    fut = ("\n".join(future) + "\n\n") if future else ""

    # pure module = future + all module-level (imports/consts) + pure funcs
    pure_code = (
        f'"""Pure helpers split from {src_path.split("/")[-1]} (shared library)."""\n'
        + fut
        + "\n\n".join(mod_level) + "\n\n\n"
        + "\n\n\n".join(span(funcs[f], lines) for f in funcs if f in pure)
        + "\n"
    )
    open(pure_out, "w").write(pure_code)

    # domain module = future + `from pure import *` + domain funcs
    domain_code = (
        f'"""Domain/LLM functions split from {src_path.split("/")[-1]} (service-owned)."""\n'
        + fut
        + f"from {pure_mod} import *  # noqa: F401,F403\n"
        + f"import {pure_mod} as _pure\n"
        + "globals().update({k: v for k, v in vars(_pure).items() if not k.startswith('__')})\n\n\n"
        + "\n\n\n".join(span(funcs[f], lines) for f in funcs if f in domain)
        + "\n"
    )
    open(domain_out, "w").write(domain_code)

    print(f"  pure ({len(pure)}): {sorted(pure)}")
    print(f"  domain ({len(domain)}): {sorted(domain)}")


if __name__ == "__main__":
    main()
