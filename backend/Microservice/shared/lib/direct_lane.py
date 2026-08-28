"""Direct Input→Output lane — pure transformation engine.

Deliberately dependency-light (pandas only) and free of DB / network calls so it
can be unit-tested in isolation. The landing record is the single source of
truth; the output BDX and the (deferred) data-model load are both projections of
it, so delivery and analytics can never silently diverge.

Pipeline:
    build_landing_record(sheets)          faithful JSON capture of an input file
    propose_sheet_routing(in_, out_)      how input tabs feed output tabs
    apply_routing(landing, routing)       gathered input rows per output sheet
    project_to_output(rows, mapping, …)   rendered output rows per output sheet

Shapes
------
landing      {"sheets": {sheet: {"columns": [...], "rows": [{col: val}]}},
              "row_count": int}

routing      {"version": 1, "mode": "pair"|"merge"|"split", "confidence": str,
              "routes": [{
                 "output_sheet": str,
                 "sources": [{"input_sheet": str}, ...],   # 1 = pair, N = merge
                 "filter": {"column": str, "equals": str} | None,  # split
              }]}

column_mapping  {output_sheet: {output_col: rule}}, rule is one of:
   {"kind": "copy",         "source": "<input col>"}
   {"kind": "const",        "value": <literal> | "@contract:<KEY>"}
   {"kind": "source_sheet"}                       # the row's source tab name (label)
   {"kind": "transform", "op": "date_reformat", "source": "<col>",
                          "from_fmt": "%Y%m%d", "to_fmt": "%d/%m/%Y"}
   {"kind": "transform", "op": "add"|"sub"|"mul"|"div", "operands": [<operand>, ...]}
       operand: {"in": "<input col>"} | {"out": "<output col>"} |
                {"const": <number>}  | {"contract": "<KEY>"}
Output columns are evaluated in declared order, so a transform may reference an
output column defined earlier (e.g. Net = Gross − Commission Amount).
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

import pandas as pd

SOURCE_SHEET_KEY = "__source_sheet__"


# ---- helpers ---------------------------------------------------------------

def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _json_safe(v: Any) -> Any:
    """Coerce a pandas/numpy cell to a JSON-serialisable scalar."""
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        # 95060.0 -> "95060" so numeric-looking ids don't drift from text twins
        return str(int(v))
    if hasattr(v, "item"):  # numpy scalar
        try:
            return v.item()
        except Exception:
            return v
    return v


def _to_number(v: Any) -> float | None:
    """Parse '10,000', '$1,500.50', '15%' → float; None when not numeric."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# ---- 1. landing capture ----------------------------------------------------

def build_landing_record(sheets: dict[str, "pd.DataFrame"]) -> dict:
    """Faithful JSON capture of an input workbook (one entry per sheet).

    Fully-empty rows are dropped so template/spacer rows don't pollute the load,
    but no column is renamed or remapped — this is a verbatim landing of input.
    """
    out_sheets: dict[str, dict] = {}
    total = 0
    for name, df in sheets.items():
        cols = [str(c) for c in df.columns]
        rows: list[dict] = []
        for rec in df.to_dict(orient="records"):
            row = {str(k): _json_safe(v) for k, v in rec.items()}
            if any(not _is_blank(val) for val in row.values()):
                rows.append(row)
        out_sheets[str(name)] = {"columns": cols, "rows": rows}
        total += len(rows)
    return {"sheets": out_sheets, "row_count": total}


def attach_supplement(landing: dict, supp_landing: dict) -> dict:
    """Attach a supplementary file's sheets to the landing as additional captured
    sheets — NO policy-number join required. A supplement file just carries extra
    data (which may relate to many policies); its sheets are added alongside the
    BDX sheets so they're captured and available. Colliding sheet names are
    suffixed. In-place on `landing`. Returns stats."""
    dst = landing.setdefault("sheets", {})
    added, rows_added = [], 0
    for name, sheet in (supp_landing.get("sheets") or {}).items():
        key = str(name)
        n = 2
        while key in dst:
            key = f"{name} (supp {n})"
            n += 1
        dst[key] = sheet
        added.append(key)
        rows_added += len(sheet.get("rows") or [])
    landing["row_count"] = int(landing.get("row_count", 0)) + rows_added
    return {"attached": True, "sheets_added": added, "rows_added": rows_added}


# ---- 2. sheet routing ------------------------------------------------------

def propose_sheet_routing(
    input_sheets: list[str], output_sheets: list[str]
) -> dict:
    """Propose how input tabs feed output tabs. Order of preference:
       name match → position match → merge (N→1) → split (1→N) → best-effort.
    The user confirms/edits this once; it is then remembered per format.
    """
    in_by_norm = {_norm(s): s for s in input_sheets}
    routes: list[dict] = []
    mode, confidence = "pair", "high"

    def pair(o: str, i: str, **extra):
        routes.append({"output_sheet": o, "sources": [{"input_sheet": i}],
                       "filter": None, **extra})

    if len(input_sheets) == len(output_sheets) and input_sheets:
        if all(_norm(o) in in_by_norm for o in output_sheets):
            for o in output_sheets:
                pair(o, in_by_norm[_norm(o)])
        else:
            confidence = "medium"
            for o, i in zip(output_sheets, input_sheets):
                pair(o, i)
    elif len(output_sheets) == 1 and len(input_sheets) > 1:
        mode, confidence = "merge", "medium"
        routes.append({"output_sheet": output_sheets[0],
                       "sources": [{"input_sheet": i} for i in input_sheets],
                       "filter": None})
    elif len(input_sheets) == 1 and len(output_sheets) > 1:
        # split: each output sheet pulls from the one input sheet; the user must
        # set a discriminator filter per route (left null = pass-through for now).
        mode, confidence = "split", "low"
        for o in output_sheets:
            pair(o, input_sheets[0])
    else:
        confidence = "low"
        for idx, o in enumerate(output_sheets):
            i = input_sheets[idx] if idx < len(input_sheets) else (
                input_sheets[-1] if input_sheets else "")
            pair(o, i)

    return {"version": 1, "mode": mode, "confidence": confidence, "routes": routes}


def _passes_filter(row: dict, filt: dict | None) -> bool:
    if not filt:
        return True
    col = filt.get("column")
    val = row.get(col)
    if "equals" in filt:
        return _norm(val) == _norm(filt["equals"])
    if "in" in filt:
        wanted = {_norm(x) for x in (filt.get("in") or [])}
        return _norm(val) in wanted
    return True


def apply_routing(landing: dict, routing: dict) -> dict[str, list[dict]]:
    """Gather the input rows that feed each output sheet. Each gathered row is a
    copy of the input row plus SOURCE_SHEET_KEY (the input tab it came from), so
    a 'source_sheet' column rule can preserve the tab identity when merging."""
    sheets = (landing or {}).get("sheets", {})
    sheets_by_norm = {_norm(k): k for k in sheets}
    result: dict[str, list[dict]] = {}
    for route in (routing or {}).get("routes", []):
        out_name = route.get("output_sheet")
        if out_name is None:
            continue
        filt = route.get("filter")
        gathered: list[dict] = []
        for src in route.get("sources", []):
            in_name = src.get("input_sheet")
            sheet = sheets.get(in_name)
            if sheet is None:
                key = sheets_by_norm.get(_norm(in_name))
                if key is not None:
                    in_name, sheet = key, sheets[key]
            if sheet is None:
                continue
            for row in sheet.get("rows", []):
                if not _passes_filter(row, filt):
                    continue
                enriched = dict(row)
                enriched[SOURCE_SHEET_KEY] = in_name
                gathered.append(enriched)
        result[out_name] = gathered
    return result


# ---- 3. projection to output ----------------------------------------------

def _operand_value(op: Any, in_row: dict, out_row: dict, constants: dict) -> float | None:
    if not isinstance(op, dict):
        return _to_number(op)
    if "const" in op:
        return _to_number(op["const"])
    if "in" in op:
        return _to_number(in_row.get(op["in"]))
    if "out" in op:
        return _to_number(out_row.get(op["out"]))
    if "contract" in op:
        return _to_number(constants.get(op["contract"]))
    return None


_DATE_INPUT_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
                       "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S")


def _date_reformat(value: Any, from_fmt: str | None, to_fmt: str | None) -> Any:
    if _is_blank(value):
        return value
    s = str(value).strip()
    to_fmt = to_fmt or "%d/%m/%Y"
    fmts = [from_fmt] if from_fmt else list(_DATE_INPUT_FORMATS)
    for f in fmts:
        try:
            return datetime.strptime(s[: len(f) + 4] if "T" in s else s, f).strftime(to_fmt)
        except (ValueError, TypeError):
            continue
    # last resort: ISO prefix
    try:
        return datetime.fromisoformat(s.replace("Z", "")).strftime(to_fmt)
    except (ValueError, TypeError):
        return value


def _apply_transform(rule: dict, in_row: dict, out_row: dict, constants: dict) -> Any:
    op = rule.get("op")
    if op == "date_reformat":
        return _date_reformat(in_row.get(rule.get("source")),
                              rule.get("from_fmt"), rule.get("to_fmt"))
    operands = rule.get("operands", [])
    vals = [_operand_value(o, in_row, out_row, constants) for o in operands]
    if op == "add":
        nums = [v for v in vals if v is not None]
        return sum(nums) if nums else None
    if op == "mul":
        if any(v is None for v in vals) or not vals:
            return None
        r = 1.0
        for v in vals:
            r *= v
        return r
    if op == "sub":
        if not vals or vals[0] is None:
            return None
        r = vals[0]
        for v in vals[1:]:
            r -= (v or 0.0)
        return r
    if op == "div":
        if not vals or vals[0] is None:
            return None
        r = vals[0]
        for v in vals[1:]:
            if not v:
                return None
            r /= v
        return r
    return None


def eval_rule(rule: Any, in_row: dict, out_row: dict, constants: dict) -> Any:
    """Evaluate one output-column rule for one row."""
    if not isinstance(rule, dict):
        return None
    kind = rule.get("kind")
    if kind == "copy":
        return in_row.get(rule.get("source"))
    if kind == "const":
        val = rule.get("value")
        if isinstance(val, str) and val.startswith("@contract:"):
            return constants.get(val.split(":", 1)[1])
        return val
    if kind == "source_sheet":
        return in_row.get(SOURCE_SHEET_KEY)
    if kind == "transform":
        return _apply_transform(rule, in_row, out_row, constants)
    return None


def project_to_output(
    routed_rows: dict[str, list[dict]],
    column_mapping: dict[str, dict[str, dict]],
    constants: dict | None = None,
) -> dict[str, list[dict]]:
    """Project gathered input rows into output rows, per output sheet, by
    evaluating each output column's rule in declared order."""
    constants = constants or {}
    out: dict[str, list[dict]] = {}
    for out_sheet, rows in routed_rows.items():
        col_rules = column_mapping.get(out_sheet, {})
        rendered: list[dict] = []
        for in_row in rows:
            o: dict[str, Any] = {}
            for out_col, rule in col_rules.items():
                o[out_col] = eval_rule(rule, in_row, o, constants)
            rendered.append(o)
        out[out_sheet] = rendered
    return out


# ---- 4. reverse mapping (output cell → source input cell) ------------------

def _gathered_provenance(landing: dict, routing: dict, out_sheet: str) -> list[tuple]:
    """(input_sheet, input_row_index) for each gathered row of an output sheet,
    in the exact order apply_routing() produces them — so a 1-based output row
    number maps straight back to the input row that produced it."""
    sheets = (landing or {}).get("sheets", {})
    by_norm = {_norm(k): k for k in sheets}
    prov: list[tuple] = []
    for route in (routing or {}).get("routes", []):
        if route.get("output_sheet") != out_sheet:
            continue
        filt = route.get("filter")
        for src in route.get("sources", []):
            in_name = src.get("input_sheet")
            real = in_name if in_name in sheets else by_norm.get(_norm(in_name))
            if real is None:
                continue
            for idx, row in enumerate(sheets[real].get("rows", [])):
                if _passes_filter(row, filt):
                    prov.append((real, idx))
    return prov


def resolve_landing_cell(
    landing: dict, routing: dict, column_mapping: dict,
    out_sheet: str, out_row: int, out_field: str,
    expect_value: Any = None,
) -> dict:
    """Reverse-map an output-stage exception coordinate to the input cell that
    produced it, so a Fix can be written back as an override on landing.data.

    ``out_row`` is 1-based (as carried by the exception). Only ``copy`` and
    ``date_reformat`` rules map to a single input cell; everything else (const,
    arithmetic transforms, source_sheet) has no single writable source.

    Returns {"ok": True, input_sheet, input_row_index (0-based), source_column,
    current_value, rule_kind, value_mismatch?} or {"ok": False, "reason": ...}."""
    rule = ((column_mapping or {}).get(out_sheet, {}) or {}).get(out_field)
    if not isinstance(rule, dict):
        return {"ok": False, "reason": f"no mapping rule for '{out_field}'"}
    kind = rule.get("kind")
    if kind == "copy" or (kind == "transform" and rule.get("op") == "date_reformat"):
        source_col = rule.get("source")
    else:
        return {"ok": False,
                "reason": f"field is '{kind}' — not editable from a single input cell"}
    if not source_col:
        return {"ok": False, "reason": "mapping rule has no source column"}

    prov = _gathered_provenance(landing, routing, out_sheet)
    i = int(out_row) - 1
    if i < 0 or i >= len(prov):
        return {"ok": False,
                "reason": f"output row {out_row} out of range (1..{len(prov)})"}
    in_sheet, in_idx = prov[i]
    current = (landing["sheets"][in_sheet]["rows"][in_idx]).get(source_col)
    res = {"ok": True, "input_sheet": in_sheet, "input_row_index": in_idx,
           "source_column": source_col, "current_value": current, "rule_kind": kind}
    # For copy rules output == input verbatim, so the current source value should
    # equal the exception's actual_value; flag (don't fail) if they diverge so the
    # caller can treat the positional match as ambiguous.
    if expect_value is not None and _norm(current) != _norm(expect_value):
        res["value_mismatch"] = True
    return res
