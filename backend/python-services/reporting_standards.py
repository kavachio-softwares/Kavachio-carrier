"""Bundled reporting standards — the ready-made Output BDX layouts.

A reporting standard is a workbook that a market body publishes saying "report
your bordereaux with THESE columns" (Lloyd's Coverholder Reporting Standards is
the one bundled today). A carrier that has no output sample of its own can adopt
one instead of uploading a file, and it becomes an ordinary, editable
``ExportTemplate`` like any other — nothing downstream is special-cased.

EVERYTHING HERE IS READ OUT OF THE WORKBOOK. No sheet name, no field name, no
CR-code and no jurisdiction is written into this file. A standard workbook is
recognised by its SHAPE:

  * a *layout tab* has ``Ref`` in A1 and ``Field`` in A2 — row 1 is the code row
    (CR0013, CR0014, …) and row 2 the human field names. One tab per
    jurisdiction, plus a superset tab.
  * a *requirements tab* has a header row containing Ref / Mand / Field /
    Territory and lists every code once, saying whether it is Mandatory or
    Conditional.

Joining the two BY CODE gives, for any jurisdiction, exactly which fields it
reports and which of them are mandatory — no fuzzy matching of territory text.

Dropping a newer workbook into ``backend/assests`` is all it takes to offer a
new version; removing one withdraws it. If the directory is empty every call
here returns empty and the UI hides the option — it never raises.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Optional

# backend/python-services/reporting_standards.py -> backend/assests
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The folder is spelled "assests" in this repo. Accept either spelling so the
# module keeps working if it is ever corrected, without a second deployment step.
_ASSET_DIRS = [os.path.join(_BACKEND_DIR, "assests"),
               os.path.join(_BACKEND_DIR, "assets")]

_REF_LABEL = "ref"
_FIELD_LABEL = "field"

# Discovery re-reads a workbook only when the file changes (path, size, mtime),
# so the list endpoint is cheap enough to call on every page load.
_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def asset_dirs() -> list[str]:
    return [d for d in _ASSET_DIRS if os.path.isdir(d)]


def _asset_files() -> list[str]:
    out: list[str] = []
    for d in asset_dirs():
        for name in sorted(os.listdir(d)):
            if name.lower().endswith((".xlsx", ".xlsm")) and not name.startswith("~$"):
                out.append(os.path.join(d, name))
    return out


def _stamp(path: str) -> tuple:
    try:
        st = os.stat(path)
        return (path, st.st_size, int(st.st_mtime))
    except OSError:
        return (path, -1, -1)


# ---------------------------------------------------------------------------
# Reading one workbook
# ---------------------------------------------------------------------------

def _cell_text(v) -> str:
    return "" if v is None else str(v).strip()


def _first_rows(ws, n: int, max_col: int) -> list[list]:
    rows: list[list] = []
    for row in ws.iter_rows(min_row=1, max_row=n, max_col=max_col, values_only=True):
        rows.append(list(row))
    while len(rows) < n:
        rows.append([])
    return rows


def _is_layout_tab(ws) -> bool:
    """A per-jurisdiction column layout: 'Ref' in A1, 'Field' in A2."""
    rows = _first_rows(ws, 2, 1)
    a1 = _cell_text(rows[0][0] if rows[0] else None).lower()
    a2 = _cell_text(rows[1][0] if rows[1] else None).lower()
    return a1 == _REF_LABEL and a2.startswith(_FIELD_LABEL)


def _requirements_header(ws) -> Optional[tuple[int, dict[str, int]]]:
    """Find the requirements dictionary's header row and its column positions.

    Scans the first few rows for one carrying a Ref column, a Field column and a
    mandatory/conditional column. Returns (row_index_1based, {role: col_index}).
    """
    for r_idx, row in enumerate(
            ws.iter_rows(min_row=1, max_row=6, max_col=12, values_only=True), start=1):
        pos: dict[str, int] = {}
        for c_idx, cell in enumerate(row or []):
            label = _cell_text(cell).lower()
            if not label:
                continue
            if label == _REF_LABEL and "ref" not in pos:
                pos["ref"] = c_idx
            elif label.startswith(_FIELD_LABEL) and "field" not in pos:
                pos["field"] = c_idx
            elif ("mand" in label or "cond" in label) and "requirement" not in pos:
                pos["requirement"] = c_idx
            elif "territor" in label and "territory" not in pos:
                pos["territory"] = c_idx
            elif "comment" in label and "comments" not in pos:
                pos["comments"] = c_idx
        if {"ref", "field", "requirement"} <= set(pos):
            return r_idx, pos
    return None


def _read_workbook(path: str) -> Optional[dict[str, Any]]:
    """Parse one candidate workbook. Returns None when it isn't a standard."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return None
    try:
        layouts: dict[str, list[dict[str, str]]] = {}
        requirements: dict[str, dict[str, str]] = {}
        for ws in wb.worksheets:
            if _is_layout_tab(ws):
                rows = _first_rows(ws, 2, ws.max_column or 1)
                fields: list[dict[str, str]] = []
                # Column 0 holds the 'Ref'/'Field' labels themselves — skip it.
                for c in range(1, len(rows[0])):
                    ref = _cell_text(rows[0][c])
                    name = _cell_text(rows[1][c]) if c < len(rows[1]) else ""
                    if not name:
                        continue
                    fields.append({"ref": ref, "field": name})
                if fields:
                    layouts[ws.title] = fields
                continue
            found = _requirements_header(ws)
            if found and not requirements:
                header_row, pos = found
                for row in ws.iter_rows(min_row=header_row + 1, max_row=ws.max_row,
                                        max_col=(max(pos.values()) + 1), values_only=True):
                    ref = _cell_text(row[pos["ref"]]) if pos["ref"] < len(row) else ""
                    if not ref:
                        continue
                    def _at(role: str) -> str:
                        i = pos.get(role)
                        return _cell_text(row[i]) if i is not None and i < len(row) else ""
                    row_req = _at("requirement")
                    prev = requirements.get(ref)
                    if prev is None:
                        requirements[ref] = {
                            "field": _at("field"),
                            "requirement": row_req,
                            "territory": _at("territory"),
                            "comments": _at("comments"),
                        }
                        continue
                    # A code can be listed once per territory it applies to.
                    # Keep ONE entry per code, gathering the territories, and —
                    # should two rows ever disagree — take the stricter reading:
                    # calling a field mandatory when the standard says so
                    # somewhere is the safe way to be wrong.
                    terr = _at("territory")
                    if terr and terr not in (prev["territory"] or "").split(", "):
                        prev["territory"] = ", ".join(
                            t for t in [prev["territory"], terr] if t)
                    if row_req.lower().startswith("mandator"):
                        prev["requirement"] = row_req
                    if not prev["comments"]:
                        prev["comments"] = _at("comments")
        if not layouts:
            return None
        return {"layouts": layouts, "requirements": requirements}
    except Exception:
        return None
    finally:
        try:
            wb.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Naming — derived from the filename, never a lookup table
# ---------------------------------------------------------------------------

_NOISE = {"xlsx", "xlsm", "reporting", "standards", "standard", "templates",
          "template", "and", "the", "risk", "premium", "premiums"}


def _version_of(stem: str) -> Optional[str]:
    """'…-V52' -> '5.2', '…_v5.2' -> '5.2'. None when the name carries none."""
    m = re.search(r"[vV]\s*(\d)[._]?(\d{1,2})\b", stem)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.search(r"[vV]\s*(\d)\b", stem)
    return m.group(1) if m else None


def _label_of(stem: str, version: Optional[str]) -> str:
    """A readable name from the filename: the words that aren't boilerplate."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", stem) if w]
    kept = [w for w in words
            if w.lower() not in _NOISE and not re.fullmatch(r"[vV]?\d[._]?\d*", w)]
    label = " ".join(w.capitalize() if w.islower() else w for w in kept) or stem
    return f"{label} v{version}" if version else label


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Which of a standard workbook's territory tabs are OFFERED. The bundled
# Lloyd's workbook ships eleven — Australia, Hong Kong, Singapore Risk, a
# "Template for all" superset and the rest — and the book written on this
# platform is only ever placed in these three, so the other eight were eight
# ways to build a template nobody here reports against.
#
# Narrowing what is OFFERED, not what can be read: a template already saved
# against another tab still resolves its layout (fields() and sheet_bytes()
# look the tab up in the workbook, not in this list).
OFFERED_JURISDICTIONS = ("Canada", "UK", "US")


def _offered(jurisdictions: list[str]) -> list[str]:
    """The workbook's tabs narrowed to the ones on offer, in the workbook's own
    order. A standard naming none of them keeps all of its own, rather than
    being reduced to an empty dropdown."""
    kept = [j for j in jurisdictions if j in OFFERED_JURISDICTIONS]
    return kept or list(jurisdictions)


def discover() -> list[dict[str, Any]]:
    """Every bundled standard, newest-named first. [] when none are bundled."""
    out: list[dict[str, Any]] = []
    for path in _asset_files():
        stamp = _stamp(path)
        with _cache_lock:
            hit = _cache.get(path)
        if not hit or hit.get("stamp") != stamp:
            parsed = _read_workbook(path)
            hit = {"stamp": stamp, "parsed": parsed}
            with _cache_lock:
                _cache[path] = hit
        parsed = hit.get("parsed")
        if not parsed:
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        version = _version_of(stem)
        out.append({
            "id": stem,
            "label": _label_of(stem, version),
            "version": version,
            "path": path,
            "jurisdictions": _offered(list(parsed["layouts"].keys())),
            "requirement_count": len(parsed["requirements"]),
        })
    return out


def get(standard_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """One standard by id; the only bundled one when `standard_id` is omitted."""
    found = discover()
    if not found:
        return None
    if standard_id:
        for s in found:
            if s["id"] == standard_id:
                return s
        return None
    return found[0]


def _parsed(standard_id: Optional[str]) -> Optional[tuple[dict, dict]]:
    s = get(standard_id)
    if not s:
        return None
    with _cache_lock:
        hit = _cache.get(s["path"])
    parsed = (hit or {}).get("parsed")
    return (s, parsed) if parsed else None


def default_jurisdiction(jurisdictions: list[str]) -> Optional[str]:
    """The pre-selected layout: the "all/every" superset tab when the workbook
    has one, else the first. Read off the tab names, not assumed."""
    if not jurisdictions:
        return None
    for j in jurisdictions:
        low = j.lower()
        if "all" in low or "every" in low:
            return j
    return jurisdictions[0]


# How much of a territory's list is wanted — and it is not the same in the two
# ways an output template gets built.
SCOPE_FULL = "full"            # everything the territory publishes
SCOPE_ESSENTIAL = "essential"  # only what it marks Mandatory


def fields(standard_id: Optional[str], jurisdiction: Optional[str], *,
           scope: str = SCOPE_FULL) -> list[dict[str, Any]]:
    """The field list one jurisdiction reports, in the workbook's own order.

    Each entry carries the code the standard knows the field by, the published
    display name, and whether the requirements dictionary calls it Mandatory —
    which is what stops a user deleting a field the standard demands.

    `scope` decides how much of it is wanted, because the standard plays two
    different parts. When a template is built FROM it, its published list IS
    the layout and all of it comes through — `full`. When a template is built
    from a CONTRACT, the contract is the layout and the standard is only there
    for the handful of columns every bordereau carries and almost no contract
    spells out: the coverholder, the insured, the period, the currency, the
    premium. Those are exactly the ones it marks Mandatory — `essential`.
    Merging its 130-odd optional columns in as well would bury the contract's
    own terms and leave both ways of building a template producing the same
    file, which is the whole reason there are two of them.
    """
    got = _parsed(standard_id)
    if not got:
        return []
    std, parsed = got
    layouts, reqs = parsed["layouts"], parsed["requirements"]
    tab = jurisdiction if jurisdiction in layouts else default_jurisdiction(list(layouts))
    if tab is None:
        return []
    out: list[dict[str, Any]] = []
    for i, f in enumerate(layouts[tab]):
        meta = reqs.get(f["ref"], {})
        requirement = (meta.get("requirement") or "").strip()
        out.append({
            "ref": f["ref"],
            "field": f["field"],
            # "Mandatory" is the standard's own word; anything else (Conditional,
            # blank, a code absent from the dictionary) is not mandatory.
            "required": requirement.lower().startswith("mandator"),
            "requirement": requirement or None,
            "territory": meta.get("territory") or None,
            "comments": meta.get("comments") or None,
            "display_order": i,
        })
    if scope == SCOPE_ESSENTIAL:
        return [f for f in out if f["required"]]
    return out


def sheet_bytes(standard_id: Optional[str], jurisdiction: Optional[str],
                rename_to: Optional[str] = None) -> tuple[bytes, str]:
    """The chosen layout tab as a standalone one-sheet workbook.

    The code row and the leading label column are dropped so the published field
    names land in row 1 — after which the ordinary ``parse_template`` +
    ``propose_template_mapping`` pipeline reads it like any uploaded sample, with
    no special-casing anywhere downstream.

    Returns (xlsx bytes, the jurisdiction actually used).
    """
    got = _parsed(standard_id)
    if not got:
        raise FileNotFoundError("no reporting standard is bundled with this service")
    std, parsed = got
    layouts = parsed["layouts"]
    tab = jurisdiction if jurisdiction in layouts else default_jurisdiction(list(layouts))
    if tab is None:
        raise KeyError("the bundled standard has no readable layouts")
    with open(std["path"], "rb") as fh:
        raw = fh.read()
    from exporter import extract_single_sheet
    return extract_single_sheet(raw, tab, drop_rows=1, drop_cols=1,
                                rename_to=rename_to), tab
