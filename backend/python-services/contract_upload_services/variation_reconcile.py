"""
variation_reconcile.py
───────────────────────
BDX-time reconciliation of enum rules' `variation_values` against the REAL
distinct values found in the uploaded data — so a check still matches when the
spreadsheet spells an authorized value differently than the contract did.

Per enum rule (value_in_set / value_not_in_set), all best-effort:
  1. Find the rule's target field + sheet(s); SKIP numeric columns (a numeric
     column has thousands of distinct values — we never SELECT DISTINCT on it).
  2. Pull the column's DISTINCT text values (capped).
  3. Drop the data values the rule ALREADY matches (equal to one of its values
     after normalization — the compiled query's own test), leaving the values the
     query does NOT match today.
  4. For those candidates ask
     the AI which, if any, are a genuine alternate spelling of an authorized
     entity. The AI answer is filtered to the EXACT candidate list (closed-list
     guard — it can never inject a value that isn't really in the data).
  5. If any are confirmed, fold them into `variation_values`, recompile the rule
     IN-MEMORY (so the current run uses it). Persisting to the DB is gated behind
     KAVACHIO_VARIATION_PERSIST=1 (default OFF — nothing is written to the DB).

NO database schema change. NO live DB write unless explicitly enabled.

Env knobs:
  KAVACHIO_VARIATION_RECONCILE   "0" disables this step entirely (default ON)
  KAVACHIO_VARIATION_PERSIST     "1" enables the (best-effort) DB write-back (default OFF)
  KAVACHIO_VARIATION_DISTINCT_CAP   max distinct values pulled per column (default 300)
  KAVACHIO_VARIATION_AI_CAP         max candidate values sent to the AI (default 80)
"""

import os
import re
import json

from contract_upload_services.rule_compiler import compile_ir, CompileError
from contract_upload_services.rule_normalizer import variation_traces_to_base

_ENUM_TEMPLATES = ("value_in_set", "value_not_in_set")
_DISTINCT_CAP = int(os.getenv("KAVACHIO_VARIATION_DISTINCT_CAP", "300"))
_AI_CAP = int(os.getenv("KAVACHIO_VARIATION_AI_CAP", "80"))
_SEED = int(os.getenv("KAVACHIO_LLM_SEED", "7"))


def _on() -> bool:
    return os.getenv("KAVACHIO_VARIATION_RECONCILE", "1") != "0"


def _persist_on() -> bool:
    return os.getenv("KAVACHIO_VARIATION_PERSIST", "0") == "1"


def _norm(s) -> str:
    """Mirror of the compiler's cell normalization (lower + strip non-alnum)."""
    return re.sub(r"[^a-z0-9]", "", str(s if s is not None else "").lower())


def _q(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def _lit(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _rule_spec(rule: dict):
    spec = rule.get("rule_spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception:
            spec = None
    return spec if isinstance(spec, dict) else None


def _looks_numeric(samples) -> bool:
    """True when EVERY non-empty sample parses as a number (commas/$/% stripped).
    Such a column is numeric → skip distinct extraction (thousands of values)."""
    seen = 0
    for v in samples or []:
        s = str(v).strip()
        if not s:
            continue
        seen += 1
        t = s.replace(",", "").replace("$", "").replace("%", "").strip()
        try:
            float(t)
        except Exception:
            return False
    return seen > 0


def _field_to_sheets(tables: dict) -> dict:
    """{column: [sheets that contain it]} — reproduces compile_ir's fan-out."""
    f2s = {}
    for sh, info in (tables or {}).items():
        for c in (info or {}).get("columns") or []:
            f2s.setdefault(c, [])
            if sh not in f2s[c]:
                f2s[c].append(sh)
    return f2s


def _sheets_with_field(tables: dict, field: str) -> list:
    return [sh for sh, info in (tables or {}).items()
            if field in ((info or {}).get("columns") or [])]


def _distinct_values(con, sheet: str, field: str, cap: int) -> list:
    q = (f'SELECT DISTINCT {_q(field)} FROM {_q(sheet)} '
         f'WHERE {_q(field)} IS NOT NULL AND TRIM({_q(field)}) <> \'\' '
         f'LIMIT {int(cap)}')
    try:
        return [r[0] for r in con.execute(q).fetchall() if r[0] is not None]
    except Exception:
        return []


def _ai_reconcile(ai, field: str, authorized: list, candidates: list) -> list:
    """Ask the AI which CANDIDATE data values are a genuine alternate spelling of
    an AUTHORIZED entity. Returns a subset of `candidates` (closed-list filtered)."""
    prompt = (
        "You decide which actual data values are alternate spellings of an "
        "authorized value. Be conservative: a wrong match hides a real violation.\n\n"
        f"Column: {field}\n"
        f"AUTHORIZED values (the only correct entities): {json.dumps(authorized, ensure_ascii=False)}\n"
        f"CANDIDATE values found in the data: {json.dumps(candidates, ensure_ascii=False)}\n\n"
        "Return STRICT JSON only: {\"matches\": [ <candidate> , ... ]}\n"
        "Include a candidate ONLY if it is the SAME entity as one authorized value "
        "(an abbreviation, partial name, or spelling variant of the same company / "
        "entity). If a candidate is a DIFFERENT entity, or you are unsure, DO NOT "
        "include it — prefer an empty list over a wrong match. Copy each candidate "
        "string EXACTLY as given; do not invent or alter values."
    )
    try:
        raw = ai(prompt, label="VarValuesReconcile", temperature=0, seed=_SEED,
                 max_output_tokens=2048)
    except Exception:
        return []
    return _parse_matches(raw, candidates)


def _parse_matches(raw, candidates: list) -> list:
    """Parse the AI JSON and keep ONLY values that are exactly in `candidates`
    (the closed-list guard — the AI can never inject a value not in the data)."""
    if not raw:
        return []
    txt = str(raw).strip()
    txt = re.sub(r"^```[a-zA-Z]*\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt).strip()
    try:
        obj = json.loads(txt)
    except Exception:
        return []
    if isinstance(obj, dict):
        arr = obj.get("matches")
    elif isinstance(obj, list):
        arr = obj
    else:
        arr = None
    if not isinstance(arr, list):
        return []
    exact = {str(c): c for c in candidates}
    by_norm = {_norm(c): c for c in candidates}
    out, seen = [], set()
    for a in arr:
        a = str(a)
        hit = exact.get(a) or by_norm.get(_norm(a))
        if hit is not None and _norm(hit) not in seen:
            seen.add(_norm(hit))
            out.append(hit)
    return out


def _persist(session, rule: dict, new_sql: str, variation_values: list) -> None:
    """Best-effort write-back to the canonical DB. Gated OFF by default; wrapped so
    it can NEVER break validation. Only runs when KAVACHIO_VARIATION_PERSIST=1."""
    rule_id = rule.get("rule_id")
    if rule_id is None:
        return
    try:
        from db import CanonicalSession
        from sqlalchemy import text as _text
        with CanonicalSession() as cs:
            cs.execute(_text(
                "UPDATE validation_rule "
                "SET rule_spec = jsonb_set(rule_spec, '{ir,params,variation_values}', "
                "    CAST(:vv AS jsonb), true), compiled_sql = :sql "
                "WHERE rule_id = :rid"),
                {"vv": json.dumps(variation_values), "sql": new_sql, "rid": rule_id})
            cs.execute(_text(
                "UPDATE rule_sql SET sql_text = :sql, updated_at = now() "
                "WHERE rule_id = :rid"),
                {"sql": new_sql, "rid": rule_id})
            cs.commit()
    except Exception:
        # Persistence is optional; the in-memory recompile already fixed this run.
        pass


def reconcile(con, tables: dict, rules: list, *, session=None, ai=None,
              persist=None) -> dict:
    """Reconcile enum rules' variation_values against the real BDX distinct values.
    Mutates rule dicts in place (rule['compiled_sql'] and rule['rule_spec']).
    `ai` is the AI callable (defaults to gemini_service.call_gemini); inject a
    fake in tests so no network call is made. Returns a small summary dict."""
    if not _on():
        return {"rules_updated": 0, "skipped": "disabled"}
    if ai is None:
        from contract_upload_services.gemini_service import call_gemini as ai
    if persist is None:
        persist = _persist_on()

    f2s = _field_to_sheets(tables)
    distinct_cache = {}
    numeric_fields = set()
    updated = 0

    for rule in rules or []:
        try:
            spec = _rule_spec(rule)
            if not spec:
                continue
            ir = spec.get("ir") or {}
            if ir.get("template") not in _ENUM_TEMPLATES:
                continue
            params = ir.get("params") or {}
            field = params.get("field")
            if not field:
                continue
            sheets = _sheets_with_field(tables, field)
            if not sheets or field in numeric_fields:
                continue

            # Numeric-column guard: never SELECT DISTINCT on a numeric column.
            samples = []
            for sh in sheets:
                samples += ((tables[sh].get("samples") or {}).get(field) or [])
            if _looks_numeric(samples):
                numeric_fields.add(field)
                continue

            # Distinct text values across every sheet that has the field (cached).
            if field in distinct_cache:
                distinct = distinct_cache[field]
            else:
                acc, seen = [], set()
                for sh in sheets:
                    if len(acc) >= _DISTINCT_CAP:
                        break
                    for v in _distinct_values(con, sh, field, _DISTINCT_CAP):
                        n = _norm(v)
                        if n and n not in seen:
                            seen.add(n)
                            acc.append(v)
                        if len(acc) >= _DISTINCT_CAP:
                            break
                distinct = acc
                distinct_cache[field] = distinct
            if not distinct:
                continue

            # The rule's current match set (variation_values, else allowed/excluded).
            vvals = params.get("variation_values")
            if not isinstance(vvals, list) or not vvals:
                vvals = list(params.get("allowed") or []) + list(params.get("excluded") or [])
            if not vvals:
                continue
            known = {_norm(v) for v in vvals if _norm(v)}

            # Candidates are the data values the compiled query does NOT match
            # today. It matches on normalized EQUALITY, so `known` is the whole
            # test — a value that merely resembles one of the rule's spellings is
            # not matched by the query and IS a candidate, which is exactly the
            # spelling this step exists to find and record.
            candidates = [d for d in distinct if _norm(d) not in known]
            if not candidates:
                continue

            # AI decides which candidates are genuinely the same authorized entity.
            picks = _ai_reconcile(ai, field, vvals, candidates[:_AI_CAP])
            # The contract-authorized values (NOT the surface variations) are the
            # ground truth a real data spelling must trace back to. Reject any AI
            # pick that shares no word / no substring with any authorized value —
            # a token-disjoint value (e.g. "Cayman" vs "Palms Insurance Company,
            # Limited") is exactly the ambiguous case where a wrong match would
            # hide a real violation. Derived from the contract's own values only.
            authorized = list(params.get("allowed") or []) + list(params.get("excluded") or [])
            foreign = [p for p in picks if authorized and not variation_traces_to_base(p, authorized)]
            if foreign:
                print(f"[VARIATION-RECONCILE] field={field!r} | REJECTED foreign "
                      f"(no trace to any authorized value): {foreign}")
            newly = [p for p in picks if _norm(p) and _norm(p) not in known
                     and not (authorized and not variation_traces_to_base(p, authorized))]
            # SCENARIO 2 LOG — AI reconciliation when variation_values did NOT match
            # the real BDX data values.
            print(f"[VARIATION-RECONCILE] field={field!r} | unmatched data values sent "
                  f"to AI: {candidates[:_AI_CAP]} | AI picked: {picks} | ADDED: {newly}")
            if not newly:
                continue

            merged = list(vvals)
            for p in newly:
                if _norm(p) not in known:
                    known.add(_norm(p))
                    merged.append(p)
            params["variation_values"] = merged
            ir["params"] = params
            spec["ir"] = ir

            try:
                new_sql = compile_ir(ir, f2s)
            except CompileError:
                continue

            spec["compiled_sql"] = new_sql
            rule["rule_spec"] = spec
            rule["compiled_sql"] = new_sql
            updated += 1
            if persist:
                _persist(session, rule, new_sql, merged)
        except Exception:
            # Never let reconciliation block validation.
            continue

    return {"rules_updated": updated}
