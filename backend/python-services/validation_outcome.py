"""What a bordereau run's validation actually checked, and the status that
honestly follows from it.

A run used to be stored as 'clean' whenever its exception list was empty — and
the list is also empty when the checks never ran. Three ways that happened,
each saved as a clean file:

  · the validation step raised (every row set aside → an empty insert → crash),
    and the render path caught it and carried on;
  · the file's sheets matched none of the sheets the setup reads (or the
    setup's sheet filter let no row through), so nothing was projected into
    the output at all;
  · rows were projected, but every one was set aside as blank or a summary row
    before a rule saw it.

`outcome` turns those into status 'not_validated' with a plain-words reason,
and the two entry builders give the review screens something to show:

  not_validated_entry  ONE critical, row-less entry saying the file was not
                       validated. Counted like any exception, so every screen
                       that looks at exception_count treats the run as needing
                       attention.
  not_checked_entry    ONE info, row-less entry listing the rules that could
                       not run (a column the setup does not fill, a column the
                       template lacks, a query that failed) and those that ran
                       on only some of their sheets. Informational: it
                       never makes a run 'has_exceptions' and is left out of
                       the stored counts — see `countable`.

Pure — no DB, no I/O — so the direct lane (direct_routes) and the canonical
export (main) share one definition.
"""
from __future__ import annotations

CLEAN, HAS_EXCEPTIONS, NOT_VALIDATED = "clean", "has_exceptions", "not_validated"
NOT_CHECKED = "not_checked"

# direct_lane.eval_rule's column-rule kinds that can write a value. Anything
# else evaluates to None on every row, so it fills nothing.
_FILLING_KINDS = ("copy", "const", "source_sheet", "transform")


def _rule_fills(rule) -> bool:
    if not isinstance(rule, dict):
        return False
    kind = rule.get("kind")
    if kind not in _FILLING_KINDS:
        return False
    if kind == "copy":
        return bool(rule.get("source")) or rule.get("default") is not None
    if kind == "const":
        return rule.get("value") not in (None, "")
    if kind == "transform":
        return bool(rule.get("op"))
    return True


def filled_cols_from_mapping(column_mapping) -> dict:
    """{output sheet: [columns]} a direct-lane column mapping writes a value
    into. Taken from the mapping's RULES (what each column is set up to
    receive), never from this file's values, so it is the same for every run
    of the setup."""
    return {sheet: [col for col, rule in (rules or {}).items() if _rule_fills(rule)]
            for sheet, rules in (column_mapping or {}).items()}


def filled_cols_from_structure(structure) -> dict:
    """{sheet: [columns]} a canonical-lane export fills: the active columns that
    resolve from a canonical field or carry a static value (the same two
    sources exporter.build_output_records reads)."""
    out = {}
    for sh in (structure or {}).get("sheets") or []:
        out[sh.get("sheet_name", "")] = [
            c.get("column_name") for c in (sh.get("columns") or [])
            if c.get("column_name") and c.get("active", True)
            and (c.get("canonical_field") or c.get("static_value") is not None)]
    return out


def _columns(structure, keep) -> dict:
    return {sh.get("sheet_name", ""): [c.get("column_name") for c in (sh.get("columns") or [])
                                       if c.get("column_name") and keep(c)]
            for sh in (structure or {}).get("sheets") or []}


def active_schema_cols(structure) -> dict:
    """{sheet: [columns]} of the columns the delivered file carries — a column
    the user switched off must not be validated, any more than it is written
    (output_template_fields.active_columns uses the same test). Template order
    is kept."""
    return _columns(structure, lambda c: c.get("active", True))


def template_schema_cols(structure) -> dict:
    """{sheet: [columns]} of every template column, switched off or not. This
    is the DuckDB table schema: a fan-out rule reads the same column on each of
    its sheets, and a sheet whose table lacked it would fail the whole rule.
    Switched-off columns are kept out of the checks through `inactive_cols`."""
    return _columns(structure, lambda c: True)


def inactive_cols(structure) -> dict:
    """{sheet: [columns]} the user switched off — not written, so never filled."""
    return {sh: cols for sh, cols in
            _columns(structure, lambda c: not c.get("active", True)).items() if cols}


def _file_sheets(landing, supplement) -> dict:
    """The landing's sheets less the setup's supplementary ones. Those are
    attached to every run's landing (direct_lane.attach_supplement, which
    suffixes a clashing name "(supp N)") but are not the file, so a file with
    no rows of its own must not read as one whose rows went missing. A file
    sheet sharing a supplement sheet's exact name is left out too; that only
    ever under-counts, which keeps the status a run had before."""
    sheets = (landing or {}).get("sheets") or {}
    names = ([str(n) for n in (((supplement or {}).get("landing") or {}).get("sheets") or {})]
             if (supplement or {}).get("enabled") else [])

    def _is_supp(key: str) -> bool:
        return any(key == n or (key.startswith(f"{n} (supp ") and key.endswith(")"))
                   for n in names)

    return {key: sh for key, sh in sheets.items() if not _is_supp(str(key))}


def input_row_count(landing, supplement) -> int:
    """Rows a direct-lane upload itself carried (`landing` is the landing
    record's data), supplementary sheets left out."""
    return sum(len((sh or {}).get("rows") or []) for sh in _file_sheets(landing, supplement).values())


def routed_row_count(landing, supplement, routing):
    """Rows on the file sheets this setup's routing reads, before its filters;
    None when the file has none of those sheets. Tells a file whose sheets the
    setup does not read (None) from one whose rows the routing filter dropped
    (a count) and from one whose routed sheets are simply empty (0)."""
    from direct_lane import _norm   # the sheet-name match apply_routing uses
    file_sheets = _file_sheets(landing, supplement)
    by_norm = {_norm(k): k for k in file_sheets}
    found = {}
    for route in (routing or {}).get("routes") or []:
        for src in route.get("sources") or []:
            name = src.get("input_sheet")
            key = name if name in file_sheets else by_norm.get(_norm(name))
            if key is not None:
                found[key] = len((file_sheets[key] or {}).get("rows") or [])
    return sum(found.values()) if found else None


def countable(exceptions) -> list:
    """The entries that count as exceptions — everything but the informational
    not-checked summary."""
    return [e for e in (exceptions or [])
            if not (isinstance(e, dict) and e.get("error_class") == NOT_CHECKED)]


_UNKNOWN = object()


def outcome(*, input_rows: int, projected_rows: int, error=None, stats=None,
            exceptions=(), routed_rows=_UNKNOWN) -> tuple[str, str | None]:
    """(status, reason) for a run. `reason` is None unless not_validated.

    `input_rows`      rows the uploaded file carried (0 when unknown)
    `projected_rows`  rows that reached the output
    `error`           the exception validation raised, if it did
    `stats`           run_validation's stats; None when validation had nothing
                      to run (no rules and no typed columns)
    `exceptions`      the run's entries (the not-checked summary is ignored)
    `routed_rows`     routed_row_count: rows on the sheets the setup reads, or
                      None when the file has none of them. Left out, a file
                      with rows and no output reads as a sheet mismatch. A
                      routed sheet that is merely empty (0) is not a failure.
    """
    if not projected_rows:
        if routed_rows is _UNKNOWN:
            routed_rows = None if input_rows else 0
        if routed_rows:
            return NOT_VALIDATED, (
                f"none of the {routed_rows:,} row(s) on the sheets this setup reads "
                f"passed its sheet filter, so no row reached the output")
        if input_rows and routed_rows is None:
            return NOT_VALIDATED, (
                f"none of the file's {input_rows:,} row(s) reached the output — its "
                f"sheets do not match the sheets this setup reads")
    if error is not None:
        detail = str(error).strip().splitlines()[0][:200] if str(error).strip() else ""
        return NOT_VALIDATED, (
            f"the checks stopped with an error before they finished"
            f"{f' ({type(error).__name__}: {detail})' if detail else ''}")
    if projected_rows and stats is not None and not stats.get("rows_validated"):
        if not stats.get("rows_total"):
            return NOT_VALIDATED, (
                f"none of the {projected_rows:,} output row(s) reached the checks")
        return NOT_VALIDATED, (
            f"every one of the {projected_rows:,} output row(s) was set aside as "
            f"blank, a summary or a repeated header, so no row was checked")
    return (HAS_EXCEPTIONS if countable(exceptions) else CLEAN), None


def not_validated_entry(reason: str) -> dict:
    """The one entry a not-validated run carries (same shape as main.py's
    per-rule not-validated notices)."""
    return {
        "severity": "critical",
        "code": NOT_VALIDATED,
        "sheet": None, "row": None, "column": None, "field": None,
        "rule_id": None,
        "rule_name": "Validation did not run",
        "reason": reason,
        "message": f"This file was NOT validated: {reason}.",
        "error_class": NOT_VALIDATED,
    }


def _shown(cols) -> str:
    return ", ".join(cols[:12]) + (f" and {len(cols) - 12} more" if len(cols) > 12 else "")


def not_checked_entry(unprocessable, partial=None) -> dict | None:
    """ONE grouped entry for the rules that could not run, or None when every
    rule ran on every sheet. `unprocessable` and `partial` are run_validation's
    `unprocessable` and `partially_checked` lists: `rules`/`columns` describe
    the rules that did not run at all, `partial` the ones that ran on only some
    of their sheets."""
    items = [u for u in (unprocessable or []) if isinstance(u, dict)]
    partial = [p for p in (partial or []) if isinstance(p, dict)]
    if not items and not partial:
        return None
    cols: list = []
    for u in items:
        for c in u.get("columns") or []:
            if c not in cols:
                cols.append(c)
    n, k = len(items), len(partial)
    parts = []
    if n:
        s = f"{n} check{'s' if n != 1 else ''} could not be run on this file"
        if cols:
            s += (f" — the columns they need are not mapped, switched off or not in "
                  f"the template: {_shown(cols)}")
        parts.append(s)
    if k:
        skipped = []
        for p in partial:
            for sh in p.get("sheets") or []:
                if sh not in skipped:
                    skipped.append(sh)
        parts.append(f"{k} check{'s' if k != 1 else ''} ran on only some sheets, "
                     f"not on {_shown(skipped)}")
    summary = "; ".join(parts)
    return {
        "severity": "info",
        "code": NOT_CHECKED,
        "sheet": None, "row": None, "column": None, "field": None,
        "rule_id": None,
        "rule_name": "Checks not run",
        "message": summary + ".",
        "reason": "\n".join(f"{u.get('rule_name') or 'Rule ' + str(u.get('rule_id'))}: "
                            f"{u.get('message') or ''}" for u in items + partial),
        "error_class": NOT_CHECKED,
        "rules": [{"rule_id": u.get("rule_id"), "rule_name": u.get("rule_name"),
                   "columns": u.get("columns") or [], "message": u.get("message")}
                  for u in items],
        "columns": cols,
        "partial": [{"rule_id": p.get("rule_id"), "rule_name": p.get("rule_name"),
                     "columns": p.get("columns") or [], "sheets": p.get("sheets") or [],
                     "message": p.get("message")} for p in partial],
    }
