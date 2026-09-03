"""Render direct-lane projected rows into an xlsx output file.

The existing exporter.generate_workbook resolves cells from canonical `policies`
via `canonical_field`; the direct lane has already produced finished output rows
keyed by output column name. This renderer writes those rows straight into the
output template, reusing exporter's style helpers so template fonts/borders are
preserved.

Also exposes `to_validation_blocks()` so the projected rows can be fed to the
existing DuckDB contract-rule validator (duckdb_validation.run_validation), which
expects the same shape exporter.build_output_records produces.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import MergedCell

import output_template_fields as otf


def to_validation_blocks(
    structure: dict, output_rows_by_sheet: dict[str, list[dict]]
) -> list[dict]:
    """Shape projected rows like exporter.build_output_records:
    [{"sheet": name, "records": [{col: val}, ...]}, ...]."""
    blocks: list[dict] = []
    for sh in structure.get("sheets", []):
        name = sh.get("sheet_name", "")
        blocks.append({"sheet": name, "records": output_rows_by_sheet.get(name, [])})
    return blocks


def _coerce_cell(val: Any) -> Any:
    if isinstance(val, datetime) and val.tzinfo is not None:
        return val.replace(tzinfo=None)
    # ISO date/datetime strings (from the landing JSON) → real date objects so the
    # output cell is a genuine date (template date format applies) rather than text.
    if isinstance(val, str):
        s = val.strip()
        if _ISO_DATE_RE.match(s):
            try:
                return datetime.strptime(s, "%Y-%m-%d").date()
            except ValueError:
                return val
        if _ISO_DT_RE.match(s):
            try:
                return datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=None)
            except ValueError:
                return val
    return val


def render_output(
    structure: dict,
    output_rows_by_sheet: dict[str, list[dict]],
    template_bytes: bytes | None = None,
) -> bytes:
    """Write the projected output rows into an xlsx. Uses the template (style
    preserving) when provided, else a plain workbook."""
    # Styling is copied from the sample workbook BY POSITION, so it can only be
    # used while the template still occupies the sample's positions. Once a
    # field has been switched off or the order changed, the two have diverged
    # and the plain writer below is the correct — and only honest — result.
    if template_bytes and not otf.diverged_from_sample(structure):
        try:
            return _render_with_template(structure, output_rows_by_sheet, template_bytes)
        except Exception:  # noqa: BLE001 — never fail the delivery on styling
            pass

    wb = Workbook()
    wb.remove(wb.active)
    for sh in structure.get("sheets", []):
        ws = wb.create_sheet(title=(sh.get("sheet_name") or "Sheet")[:31])
        # The user's own order, and only the fields still switched on.
        cols = otf.active_columns(sh)
        for pos, c in enumerate(cols, start=1):
            # The user's name for the column, not the internal key.
            ws.cell(row=1, column=pos, value=otf.header_of(c))
        rows = output_rows_by_sheet.get(sh.get("sheet_name", ""), [])
        for r, row in enumerate(rows, start=2):
            for pos, c in enumerate(cols, start=1):
                name = c.get("column_name")
                if not name:
                    continue
                val = _coerce_cell(row.get(name))
                if val is not None:
                    ws.cell(row=r, column=pos, value=val)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()



def _render_with_template(
    structure: dict,
    output_rows_by_sheet: dict[str, list[dict]],
    template_bytes: bytes,
) -> bytes:
    from exporter import (_copy_cell_style, _find_style_source,
                          _find_summary_style_source, _is_non_data_row_values)

    wb = load_workbook(io.BytesIO(template_bytes))
    existing = {ws.title: ws for ws in wb.worksheets}

    for sh in structure.get("sheets", []):
        cols = sorted(sh.get("columns", []), key=lambda c: c.get("column_index", 0))
        sheet_name = sh.get("sheet_name") or ""
        if sheet_name in existing:
            ws = existing[sheet_name]
        else:
            ws = wb.create_sheet(title=sheet_name[:31] or "Sheet")

        header_row_1b = (sh.get("header_row") or 0) + 1
        data_start = (sh.get("data_start_row") or 1) + 1

        # Ensure every tracked column has a non-blank header cell AT THE
        # TEMPLATE'S OWN HEADER ROW, whether this sheet is brand new or reused
        # from the template file. Previously this only ran for a brand-new
        # sheet — a REUSED sheet's header row was never touched at all, so a
        # template whose header sits below a leading annotation row (e.g. row
        # 2, not row 1) rendered with a genuinely blank row 1 in the
        # downloaded output and no way to notice the real header had moved.
        for c in cols:
            col_idx = c.get("column_index", 0) + 1
            cell = ws.cell(row=header_row_1b, column=col_idx)
            # Fill a blank header — and overwrite the sample's own wording when
            # the user has renamed the column, because then their word is the
            # point. An untouched column keeps the sample's exact header text.
            if cell.value is None or not str(cell.value).strip() or otf.is_renamed(c):
                cell.value = otf.header_of(c)

        # Free up merged ranges in the data area so every cell can be written.
        for mr in list(ws.merged_cells.ranges):
            if mr.min_row >= data_start:
                ws.unmerge_cells(str(mr))

        # Both donor maps read the sample's VALUES to tell data rows from
        # summary rows, so they must be captured BEFORE the clear loop below
        # blanks the data region.
        style_template = {
            c.get("column_index", 0) + 1: _find_style_source(
                ws, header_row_1b, data_start, c.get("column_index", 0) + 1)
            for c in cols
        }
        # Summary rows pass through into the output as ordinary rows; they get
        # the sample's SUMMARY-row style (bold kept — that bold belongs to
        # them), not the data-row style.
        summary_style_template = {
            c.get("column_index", 0) + 1: _find_summary_style_source(
                ws, data_start, c.get("column_index", 0) + 1)
            for c in cols
        }
        sample_row_height = ws.row_dimensions[data_start].height

        # Clear stale data rows.
        for r in range(data_start, (ws.max_row or data_start) + 1):
            for c in cols:
                cell = ws.cell(row=r, column=c.get("column_index", 0) + 1)
                if not isinstance(cell, MergedCell):
                    cell.value = None

        rows = output_rows_by_sheet.get(sheet_name, [])
        out_row = data_start
        for row in rows:
            # A summary/totals row keeps the sample's summary style (its bold
            # is its own); a data row gets the data style. Judged from the
            # row's own values, same categories as row_classifier.
            row_is_summary = _is_non_data_row_values(
                [row.get(c.get("column_name")) for c in cols if c.get("column_name")])
            for c in cols:
                col_idx = c.get("column_index", 0) + 1
                cell = ws.cell(row=out_row, column=col_idx)
                if isinstance(cell, MergedCell):
                    continue
                name = c.get("column_name")
                val = _coerce_cell(row.get(name)) if name else None
                if val is not None:
                    cell.value = val
                donor = ((summary_style_template.get(col_idx) if row_is_summary else None)
                         or style_template.get(col_idx))
                # strip_bold only when the donor is the HEADER cell — data rows
                # keep the template's original style; the header's bold is not
                # part of it. A summary donor's row is never the header row, so
                # summary rows keep their bold. Exception highlighting still
                # overrides fills later.
                _copy_cell_style(
                    donor, cell,
                    strip_bold=(getattr(donor, "row", None) == header_row_1b))
            if sample_row_height is not None:
                ws.row_dimensions[out_row].height = sample_row_height
            out_row += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


# ─── Summary-row totals: keep them CORRECT across Fix & re-render ────────────
#
# The direct lane passes a bordereau's summary/totals rows through as ordinary
# rows, so a "Totals" cell holds whatever STATIC number the input file carried.
# When the reviewer fixes a data cell in Exception Triage and re-renders, the
# data changes but that static total does not — the downloaded workbook's
# summary math is silently wrong.
#
# The original input file's own formulas are NOT available here (the landing
# keeps only the parsed values), so the relationship is DETECTED instead of
# replayed: on the UNCORRECTED projection — where the input's totals are still
# self-consistent — a summary cell that equals the sum of the numeric column
# above it is provably a column total. Those cells are then patched in the
# final workbook to carry a real Excel formula (=SUM over the output's own
# rows) PLUS a cached value recomputed from the CORRECTED rows — exactly the
# <f> + <v> pair Excel itself saves. Excel recalculates live on open; the
# in-app viewers load with data_only=True, so the screen shows only the value.

_NUM_STRIP_RE = re.compile(r"[\s,$%]")


def _totals_num(v):
    """Lenient numeric read for totals math; None when not a number."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _NUM_STRIP_RE.sub("", str(v))
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        return -float(s) if neg else float(s)
    except ValueError:
        return None


def _totals_close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.011, abs(b) * 1e-6)


def detect_summary_totals(structure: dict, projected: dict) -> dict:
    """Find summary cells that are COLUMN TOTALS, on the uncorrected projection.

    Returns {sheet_name: {(row_idx, column_name): {"segs": ((a, b), ...),
    "exact": bool}}} — 0-based indices into that sheet's projected row list.
    "exact" means the cell equals (within tolerance) the sum of the column over
    those data-row segments — a proven total. A non-exact entry records the
    CLOSEST candidate range instead (the input's total may already be stale);
    it is only acted on when the template's own formula confirms the column
    (see inject_summary_formulas). Candidates per summary row: the SECTION
    above it (since the previous summary row — subtotals) and ALL data rows
    above (grand totals)."""
    from exporter import _is_non_data_row_values
    out: dict = {}
    for sh in structure.get("sheets", []) or []:
        name = sh.get("sheet_name") or ""
        rows = projected.get(name) or []
        if len(rows) < 2:
            continue
        cols = [c.get("column_name") for c in (sh.get("columns") or [])
                if c.get("column_name")]
        is_summary = [
            _is_non_data_row_values([r.get(c) for c in cols]) for r in rows]
        found: dict = {}
        for i, r in enumerate(rows):
            if not is_summary[i]:
                continue
            prev = i - 1
            while prev >= 0 and not is_summary[prev]:
                prev -= 1
            section = [j for j in range(prev + 1, i) if not is_summary[j]]
            everything = [j for j in range(i) if not is_summary[j]]
            for c in cols:
                target = _totals_num(r.get(c))
                if target is None:
                    continue
                best = None                      # (abs diff, segments, exact)
                for cand in (section, everything):
                    if len(cand) < 2:
                        continue
                    vals = [_totals_num(rows[j].get(c)) for j in cand]
                    nums = [v for v in vals if v is not None]
                    if len(nums) < 2:
                        continue
                    # contiguous segments — a grand total's span may cross an
                    # interior subtotal row, which must stay OUT of the SUM
                    # (it would double-count its section)
                    segs, start = [], cand[0]
                    for prev_j, j in zip(cand, cand[1:]):
                        if j != prev_j + 1:
                            segs.append((start, prev_j)); start = j
                    segs.append((start, cand[-1]))
                    diff = abs(sum(nums) - target)
                    exact = _totals_close(sum(nums), target)
                    if best is None or diff < best[0]:
                        best = (diff, tuple(segs), exact)
                    if exact:
                        break
                if best is not None:
                    # Exact match = a PROVEN column total. A non-exact best-fit
                    # is kept too (the input's total may already be stale) but
                    # only acted on when the OUTPUT TEMPLATE's own formula
                    # confirms the column is a total (see
                    # inject_summary_formulas).
                    found[(i, c)] = {"segs": best[1], "exact": best[2]}
        if found:
            out[name] = found
    return out


def _fmt_num(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else repr(round(x, 10))


# Same-column aggregate: =FN(X2:X51) or =SUBTOTAL(code, X2:X51). Re-anchorable
# to the output's own rows, and its cached value is computable in Python.
_CELL_REF_RE = re.compile(r"(\$?)([A-Z]{1,3})(\$?)(\d+)")


def _shift_relative_rows(f: str, delta: int) -> str:
    """Move a formula's RELATIVE row references by `delta` — exactly what Excel
    does when the formula's row moves. Absolute rows ($5) stay put. delta=0
    returns the text byte-for-byte unchanged."""
    if not delta:
        return f
    def sub(m):
        if m.group(3) == "$":
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{int(m.group(4)) + delta}"
    return _CELL_REF_RE.sub(sub, f)


def _template_formula_map(template_bytes, structure: dict) -> dict:
    """The template sheet's formulas, organised by SECTION.

    A BDX template is authored in sections: runs of data rows (whose computed
    columns carry per-row formulas — filled down, relative refs per row,
    absolute refs like $Y$1 pinned) closed by a summary row (aggregating that
    section). Returns, per sheet:

        {"sections": [{"data_rows": [excel rows...],
                       "summary_row": excel row | None,
                       "summary_formulas": {col: text}}, ...],
         "grand": {"row": excel row, "formulas": {col: text}} | None}

    Only SUMMARY-row formulas are extracted for injection — data rows ship as
    VALUES (the input's own reported numbers). Data rows are still read here
    solely to find the section boundaries.

    Classification is by the row's OWN cells: a row whose formula cells are
    same-column aggregates (SUM/SUBTOTAL/AVERAGE/…) over other rows is a
    summary row; anything else with values/formulas is a data row. A trailing
    summary row whose range reaches back past its own section (e.g.
    SUBTOTAL(9,X3:X39) spanning everything) is the GRAND total."""
    out: dict = {}
    if not template_bytes:
        return out
    try:
        twb = load_workbook(io.BytesIO(template_bytes))
    except Exception:
        return out
    from openpyxl.utils import get_column_letter
    for sh in structure.get("sheets", []) or []:
        name = sh.get("sheet_name") or ""
        if name not in twb.sheetnames:
            continue
        ws = twb[name]
        data_start = (sh.get("data_start_row") or 1) + 1
        cols = {c.get("column_index", 0) + 1: c.get("column_name")
                for c in (sh.get("columns") or []) if c.get("column_name")}
        letters = {ci: get_column_letter(ci) for ci in cols}

        def _agg_range(f: str, letter: str):
            """(start_row, end_row) when `f` is a same-column aggregate."""
            m = re.match(
                r"^=\s*(?:SUM|AVERAGE|COUNT|COUNTA|MIN|MAX|SUBTOTAL\s*\(\s*\d+\s*,)"
                r"\s*\(?\s*\$?" + letter + r"\$?(\d+)\s*:\s*\$?"
                + letter + r"\$?(\d+)\s*\)+\s*$", f, re.IGNORECASE)
            return (int(m.group(1)), int(m.group(2))) if m else None

        sections, cur_data = [], []
        grand = None
        for r in range(data_start, (ws.max_row or data_start) + 1):
            fcells = {}
            n_vals = 0
            for ci, cname in cols.items():
                v = ws.cell(row=r, column=ci).value
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                n_vals += 1
                if isinstance(v, str) and v.startswith("="):
                    fcells[cname] = (v, ci)
            if not n_vals:
                continue
            aggs = {cn: _agg_range(v, letters[ci])
                    for cn, (v, ci) in fcells.items()
                    if _agg_range(v, letters[ci])}
            if aggs and len(aggs) == len(fcells):
                # summary row. Grand = its range starts before this section.
                sect_first = cur_data[0] if cur_data else r
                spans_back = any(a and a[0] < sect_first - 1 for a in aggs.values())
                entry = {cn: v for cn, (v, _ci) in fcells.items()}
                if spans_back or (not cur_data and sections):
                    grand = {"row": r, "formulas": entry}
                else:
                    sections.append({"data_rows": list(cur_data),
                                     "summary_row": r,
                                     "summary_formulas": entry})
                    cur_data = []
            else:
                cur_data.append(r)
        if cur_data:
            sections.append({"data_rows": list(cur_data),
                             "summary_row": None, "summary_formulas": {}})
        if sections or grand:
            out[name] = {"sections": sections, "grand": grand}
    return out


def inject_summary_formulas(xlsx_bytes: bytes, structure: dict,
                            projected: dict, summary_map: dict,
                            template_bytes: bytes | None = None) -> bytes:
    """Patch the RENDERED workbook so each detected totals cell carries
    =SUM(<its own column's output rows>) plus a cached value recomputed from
    the (corrected) projected rows. Pure post-processing on the saved bytes —
    runs LAST (after exception highlighting, which re-saves the file and would
    drop anything openpyxl can't round-trip). Never raises: any failure
    returns the input bytes unchanged."""
    if not summary_map:
        return xlsx_bytes
    import zipfile
    from openpyxl.utils import get_column_letter
    tmpl_map = _template_formula_map(template_bytes, structure)
    try:
        zin = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
        # sheet name -> xl/worksheets/sheetN.xml, via workbook.xml + its rels
        wbxml = zin.read("xl/workbook.xml").decode("utf-8")
        rels = zin.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        # Attribute ORDER inside a <Relationship> is not fixed (openpyxl writes
        # Target before Id), so extract each attribute independently per element rather
        # than in one ordered pattern. Target may be package-absolute
        # ("/xl/worksheets/sheet1.xml") or workbook-relative
        # ("worksheets/sheet1.xml").
        rid_to_target: dict[str, str] = {}
        for rel in re.findall(r"<Relationship\b[^>]*>", rels):
            rid = re.search(r'\bId="([^"]+)"', rel)
            tgt = re.search(r'\bTarget="([^"]+)"', rel)
            if rid and tgt:
                t = tgt.group(1)
                rid_to_target[rid.group(1)] = (
                    t.lstrip("/") if t.startswith("/") else "xl/" + t)
        sheet_path: dict[str, str] = {}
        for m in re.finditer(r"<sheet\b[^>]*>", wbxml):
            nm = re.search(r'\bname="([^"]+)"', m.group(0))
            rid = re.search(r'\br:id="([^"]+)"', m.group(0))
            if nm and rid and rid.group(1) in rid_to_target:
                sheet_path[nm.group(1)] = rid_to_target[rid.group(1)]

        patched: dict[str, bytes] = {}
        for sh in structure.get("sheets", []) or []:
            name = sh.get("sheet_name") or ""
            targets = summary_map.get(name)
            path = sheet_path.get(name)
            if not targets or not path:
                continue
            rows = projected.get(name) or []
            data_start = (sh.get("data_start_row") or 1) + 1   # excel 1-based
            col_letter = {c.get("column_name"): get_column_letter(
                              c.get("column_index", 0) + 1)
                          for c in (sh.get("columns") or []) if c.get("column_name")}
            tm = tmpl_map.get(name)
            if not tm:
                continue    # template has no formulas here — values pass through
            xml = zin.read(path).decode("utf-8")

            # OUTPUT sections, same walk as the template's: runs of data rows
            # closed by a summary row (classified from the projected values).
            from exporter import _is_non_data_row_values
            colnames = [c.get("column_name") for c in (sh.get("columns") or [])
                        if c.get("column_name")]
            out_sections, cur = [], []
            out_summary_ris = set()
            for ri, rrow in enumerate(rows):
                if _is_non_data_row_values([rrow.get(c) for c in colnames]):
                    out_summary_ris.add(ri)
                    out_sections.append({"data": list(cur), "summary": ri})
                    cur = []
                else:
                    cur.append(ri)
            if cur:
                out_sections.append({"data": list(cur), "summary": None})

            # queue of {excel ref -> (formula text, cached value)} then ONE
            # regex pass over the sheet XML.
            plan: dict[str, tuple] = {}

            def _cache(ri, cname):
                return _totals_num(rows[ri].get(cname))

            # ── pair template sections with output sections, in order ──────
            # DATA ROWS ARE VALUES, BY DESIGN: they are the input's own
            # reported numbers — the ones validation actually ran against —
            # so the template's per-row computed-column formulas are NOT
            # replayed over them (a recomputed cell could silently disagree
            # with the validated value). Only SUMMARY rows carry formulas.
            for tsec, osec in zip(tm["sections"], out_sections):
                # section summary: the author's formula with its range
                # re-anchored to THIS output section's own rows
                if osec["summary"] is None:
                    continue
                srow = data_start + osec["summary"]
                first = data_start + (osec["data"][0] if osec["data"] else osec["summary"])
                last = data_start + (osec["data"][-1] if osec["data"] else osec["summary"])
                for cname, text in (tsec.get("summary_formulas") or {}).items():
                    letter = col_letter.get(cname)
                    if not letter:
                        continue
                    newf = re.sub(
                        r"(\$?" + letter + r"\$?)\d+(\s*:\s*\$?" + letter
                        + r"\$?)\d+",
                        lambda m: f"{m.group(1)}{first}{m.group(2)}{last}",
                        text, count=1)
                    nums = [v for v in (_cache(j, cname) for j in osec["data"])
                            if v is not None]
                    fl = text.upper()
                    if "AVERAGE" in fl:
                        val = sum(nums) / len(nums) if nums else None
                    elif "COUNT" in fl:
                        val = float(len(nums))
                    elif "MIN(" in fl:
                        val = min(nums) if nums else None
                    elif "MAX(" in fl:
                        val = max(nums) if nums else None
                    else:                      # SUM / SUBTOTAL(9|109)
                        val = float(sum(nums)) if nums else None
                    if val is None:
                        continue
                    plan[f"{letter}{srow}"] = (newf.lstrip("="), val)

            # ── grand total: the LAST output summary row after the paired
            # sections, range spanning the whole output data block ────────────
            n_paired = min(len(tm["sections"]), len(out_sections))
            spare = [sec["summary"] for sec in out_sections[n_paired:]
                     if sec["summary"] is not None]
            if tm.get("grand") and spare:
                gri = spare[-1]
                grow = data_start + gri
                d_first = data_start
                d_last = data_start + max((sec["data"][-1] for sec in out_sections
                                           if sec["data"]), default=gri) 
                for cname, text in tm["grand"]["formulas"].items():
                    letter = col_letter.get(cname)
                    if not letter:
                        continue
                    newf = re.sub(
                        r"(\$?" + letter + r"\$?)\d+(\s*:\s*\$?" + letter
                        + r"\$?)\d+",
                        lambda m: f"{m.group(1)}{d_first}{m.group(2)}{d_last}",
                        text, count=1)
                    nums = [v for ri2 in range(len(rows))
                            if ri2 not in out_summary_ris
                            for v in [_cache(ri2, cname)] if v is not None]
                    val = float(sum(nums)) if nums else None
                    if val is None:
                        continue
                    plan[f"{letter}{grow}"] = (newf.lstrip("="), val)

            if not plan:
                continue

            def _x(t):
                return (t.replace("&", "&amp;").replace("<", "&lt;")
                        .replace(">", "&gt;"))

            # ONE pass: rewrite every planned NUMERIC cell (t="n" or no t)
            cellpat = re.compile(
                r'<c\b([^>]*?)\br="([A-Z]+\d+)"([^>]*)>(?:<v>[^<]*</v>)?</c>')

            def _sub(m):
                ref = m.group(2)
                hit = plan.get(ref)
                attrs = m.group(1) + m.group(3)
                if not hit or re.search(r'\bt="(?!n")', attrs):
                    return m.group(0)
                f, v = hit
                return (f'<c{m.group(1)}r="{ref}"{m.group(3)}>'
                        f"<f>{_x(f)}</f><v>{_fmt_num(v)}</v></c>")

            xml = cellpat.sub(_sub, xml)
            patched[path] = xml.encode("utf-8")

        if not patched:
            return xlsx_bytes
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = patched.get(item.filename) or zin.read(item.filename)
                zout.writestr(item, data)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — totals repair must never break delivery
        log.warning("summary-formula injection skipped: %s", exc)
        return xlsx_bytes
