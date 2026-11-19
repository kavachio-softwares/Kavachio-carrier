"""
version_diff_sheet.py
─────────────────────
The Previous Submission Reconciliation (version_diff.py) as people read it:
the "Previous Submission" tab of the exceptions screen, and the CSV.

The tab is laid out like the bordereau itself — the same sheet-by-sheet grid
as BDX Review — but rows are paired on the business key, never by position:
  - which version this file is, which version it was compared with, and how
    a row is recognised in both;
  - the counts: All, then the task's four outcomes — New, Changed,
    Disappeared, Restated (the rest of All is unchanged);
  - a totals warning only when an amount's change is not explained by the
    rows;
  - every row of this file in its own order, with its outcome; a changed
    value carries the value it had; a row only the accepted version had is
    put back where it was, as a file comparison shows a deleted line.

Decided 19 Oct 2026: the comparison lives on screen and the downloaded Excel
is the bordereau alone. Outputs made before then still carry a "Previous
submission" sheet; `strip_sheet` takes it out of the file as it is
downloaded, without opening the workbook.

The CSV keeps one line per changed value.

No database and no web imports, so it can be tested on its own.
"""
from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

import version_diff as vd

# The sheet outputs made before 19 Oct 2026 carry (see strip_sheet), and the
# first line of the CSV.
SHEET_NAME = "Previous submission"
TITLE = "Previous submission reconciliation"
_STAMP = "Kavachio previous submission"

# Every outcome, in the task's order, then the rest of All.
ORDER = (vd.NEW, vd.CHANGED, vd.DISAPPEARED, vd.RESTATED, vd.UNCHANGED)
# The colours of each outcome: its label and a new or missing row (fill,
# ink), and a value that changed (cell). None of them is the light red or
# amber the BDX grids use to mark exceptions.
STYLE = {
    vd.NEW: {"fill": "#F3E8FD", "ink": "#6A1B9A", "cell": "#F3E8FD"},
    vd.CHANGED: {"fill": "#E8F0FE", "ink": "#1A56DB", "cell": "#C6DAFC"},
    vd.DISAPPEARED: {"fill": "#FEF7E0", "ink": "#8A5A00", "cell": "#FEF7E0"},
    vd.RESTATED: {"fill": "#FDECEA", "ink": "#8C1D18", "cell": "#F8D0CB"},
    vd.UNCHANGED: {"fill": "#E6F4EA", "ink": "#1E7B34", "cell": None},
}
# What each count means ({v} = "version 3"), behind the ⓘ on its box.
MEANING = {
    "all": "Every row of this file, plus the rows that disappeared since {v}.",
    vd.NEW: "In this file, not in {v}: a new policy, endorsement or other new row.",
    vd.CHANGED: "The same row as in {v}, but details other than amounts differ.",
    vd.DISAPPEARED: "In {v}, not in this file.",
    vd.RESTATED: "The same row as in {v}, but an amount differs.",
    vd.UNCHANGED: "The same row as in {v}, and nothing differs.",
}
# The colour key under the grid, a few words each.
LEGEND = {
    vd.NEW: "not in {v}",
    vd.CHANGED: "details differ",
    vd.DISAPPEARED: "in {v}, not here",
    vd.RESTATED: "amount differs",
    vd.UNCHANGED: "no change",
}
# The CSV: who and which row first, then the value (was → now) and why; the
# rest of the key after, for filtering.
LINE_COLUMNS = ("Outcome", "Policy", "Insured", "Row in this file", "Row in accepted version",
                "Field", "Accepted version", "This file", "Difference", "What happened",
                "Transaction type", "Effective date")


def _num(v) -> str:
    return "" if v is None else f"{float(v):,.2f}"


def _signed(v) -> str:
    v = float(v or 0)
    return ("+" if v > 0 else "−" if v < 0 else "") + _num(abs(v))


def _when(v) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _received(info: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(info["received_at"]) if info.get("received_at") else None
    except (TypeError, ValueError):
        return None


# ── what the comparison found ───────────────────────────────────────────────

def money_fields(result: dict) -> dict:
    """{money column: data-model field or None} over every compared sheet."""
    out: dict = {}
    for s in result.get("sheets") or []:
        out.update(s.get("money_fields") or {c: None for c in s.get("money_columns") or []})
    return out


def _amounts(cols: list[str], fields: dict) -> list[str]:
    """The amount columns among `cols` — every one the setup maps to a money
    field, rates (commission %) left out: a rate is never added up."""
    return [c for c in cols if not vd._is_rate(c, fields.get(c))]


def _matched_on(result: dict) -> Optional[str]:
    """'policy number, transaction type and transaction effective date' — the
    key parts the compared sheets used, as words in a sentence."""
    used = []
    for s in result.get("sheets") or []:
        for p in s.get("parts_used") or []:
            if p not in used:
                used.append(p)
    words = [vd.rk.PART_WORDS[p].lower() for p in used]
    if not words:
        return None
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def version_word(result: dict) -> str:
    """'version 3' — the version this file was compared with."""
    acc = (result.get("accepted") or {}).get("version")
    return f"version {acc}" if acc else "the last version sent to the carrier"


def about_for(result: dict) -> list[str]:
    """Two plain sentences: which versions are compared, and when a row
    counts as the same row in both."""
    this, acc = result.get("this") or {}, result.get("accepted") or {}

    def received(info: dict) -> str:
        when = _received(info)
        return f" (received {when:%d %b %Y})" if when else ""

    out = []
    if this.get("version") and acc.get("version"):
        out.append(f"Version {this['version']}{received(this)} compared with version "
                   f"{acc['version']}, the last version sent to the carrier{received(acc)}.")
    key = _matched_on(result)
    if key:
        out.append(f"Rows match when their {key} are the same.")
    return out


def warnings(result: dict) -> list[str]:
    """One line per amount whose change the rows do not explain (its control
    total does not balance); none when every difference is in the rows."""
    if result.get("state") != "compared":
        return []
    s = result.get("summary") or {}
    name = version_word(result)
    fields = money_fields(result)
    money = {c: m for c, m in (s.get("money") or {}).items()
             if m.get("accepted") or m.get("this")}
    out = []
    for c in _amounts(list(money), fields):
        m = money[c]
        gap = m.get("control_gap") or 0
        if gap:
            out.append(f"Check the totals: {c} is {_num(m.get('this') or 0)} in this file and "
                       f"{_num(m.get('accepted') or 0)} in {name}, and {_signed(gap)} of that "
                       f"difference is not explained by the rows.")
    return out


# ── the bordereau view ──────────────────────────────────────────────────────

def _was_now(item: dict, show) -> str:
    """'Gross Premium: 9,800.00 → 10,800.00; Insured: A → B'."""
    bits = []
    for c, m in (item.get("money") or {}).items():
        if m.get("was") is not None and m.get("now") is not None:
            bits.append(f"{c}: {show(c, m['was']) or '(blank)'} → {show(c, m['now']) or '(blank)'}")
    for c, ch in (item.get("changes") or {}).items():
        bits.append(f"{c}: {show(c, ch.get('was')) or '(blank)'} → "
                    f"{show(c, ch.get('now')) or '(blank)'}")
    return "; ".join(bits)


def _entry(item: Optional[dict], values: dict, row: Optional[int],
           accepted_row: Optional[int], gone: bool, show) -> dict:
    """One line of the view: a row of this file, or one only the accepted
    version had (`gone`). `note` says what happened to it — then, for a
    changed row, which values changed, since a changed cell can be far to
    the right."""
    if item is None:
        return {"outcome": vd.UNCHANGED, "row": row, "accepted_row": accepted_row,
                "values": values, "was": {}, "note": "", "gone": gone}
    was = {c: m["was"] for c, m in (item.get("money") or {}).items()
           if m.get("was") is not None and m.get("now") is not None}
    was.update({c: ch.get("was") for c, ch in (item.get("changes") or {}).items()})
    note = item.get("detail") or ""
    if was:
        note = f"{note}\nWhat changed: {_was_now(item, show)}".strip()
    return {"outcome": item.get("outcome"), "row": item.get("row"),
            "accepted_row": item.get("accepted_row"), "values": values, "was": was,
            "note": note, "gone": gone}


def _shower(view: dict, fields: dict):
    """show(column, value) → the text the tab shows: amounts as 1,234.50,
    dates the setup reads as dates as 15 Aug 2025, whole numbers without
    '.0', blank as ''."""
    dated = set(view.get("date_columns") or [])
    amount = {c for c in fields if not vd._is_rate(c, fields.get(c))}

    def show(c, v) -> str:
        if v is None or (isinstance(v, str) and not v.strip()):
            return ""
        if c in amount:
            n = vd._number(v)
            if n is not None:
                return _num(n)
        if c in dated:
            d = v if isinstance(v, date) else _when(v) if isinstance(v, str) else None
            if d is not None:
                return d.strftime("%d %b %Y")
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    return show


def view_tables(result: dict) -> list[dict]:
    """Per compared sheet: {sheet, columns, entries}. Every row of this file
    in its own order (repeated heading lines left out); a row only the
    accepted version had is put back right after the row that came before it
    there."""
    items = result.get("rows") or []
    info = {s.get("sheet"): s for s in result.get("sheets") or []}
    out = []
    for sheet, v in (result.get("view") or {}).items():
        show = _shower(v, (info.get(sheet) or {}).get("money_fields") or {})
        mine = [x for x in items if x.get("sheet") == sheet]
        by_row = {x["row"]: x for x in mine if x.get("row")}
        pairs = list(v.get("matched") or [])
        this_to_acc = {c: b for c, b in pairs}
        partner = {b: c for c, b in pairs}             # accepted row → this row, 0-based
        for x in mine:
            if x.get("row") and x.get("accepted_row"):
                partner[x["accepted_row"] - 1] = x["row"] - 1
        keys = sorted(partner)
        after: dict = defaultdict(list)
        for x in sorted((x for x in mine if not x.get("row") and x.get("accepted_row")),
                        key=lambda x: x["accepted_row"]):
            at = bisect_left(keys, x["accepted_row"] - 1)
            after[partner[keys[at - 1]] if at else -1].append(x)
        cols = list(v.get("columns") or [])
        acc_col = v.get("accepted_column") or {}
        acc_rows = v.get("accepted_rows") or []

        def gone(x):
            i = x["accepted_row"] - 1
            row = acc_rows[i] if 0 <= i < len(acc_rows) else {}
            return _entry(x, {c: row.get(acc_col.get(c) or c) for c in cols},
                          None, x["accepted_row"], True, show)

        entries = [gone(x) for x in after[-1]]
        for i, row in enumerate(v.get("this_rows") or []):
            x = by_row.get(i + 1)
            if x is None and i not in this_to_acc:
                continue
            acc = this_to_acc.get(i)
            entries.append(_entry(x, {c: row.get(c) for c in cols}, i + 1,
                                  acc + 1 if acc is not None else None, False, show))
            entries.extend(gone(y) for y in after.get(i, []))
        out.append({"sheet": sheet, "columns": cols, "entries": entries, "show": show})
    return out


def screen(result: dict) -> dict:
    """What the tab shows, worked out once per comparison (the routes page
    and filter it): the sentences on top, the counts, the colour key, and
    per sheet its columns and rows — each cell as the text shown, and for a
    changed cell the text it had ({column index: text})."""
    state = result.get("state")
    v = version_word(result)
    counts = (result.get("summary") or {}).get("outcomes") or {}
    out = {
        "state": state,
        "message": (None if state == "compared"
                    else (result.get("summary") or {}).get("plain") or "Not compared."),
        "this": result.get("this") or {},
        "accepted": result.get("accepted") or None,
        "about": about_for(result) if state == "compared" else [],
        "warnings": warnings(result),
        "total": sum(counts.get(k, 0) for k in ORDER),
        "all_meaning": MEANING["all"].format(v=v),
        "outcomes": [{"code": k, "word": vd.OUTCOME_WORDS[k], "count": counts.get(k, 0),
                      "meaning": MEANING[k].format(v=v), "legend": LEGEND[k].format(v=v),
                      **STYLE[k]} for k in ORDER],
        "sheets": [],
    }
    tables = {t["sheet"]: t for t in view_tables(result)} if state == "compared" else {}
    for s in result.get("sheets") or []:
        t = tables.get(s.get("sheet"))
        if t is None:
            out["sheets"].append({"sheet": s.get("sheet"), "columns": [], "entries": [],
                                  "counts": {}, "message": s.get("state_text") or "Not compared."})
            continue
        cols, show = t["columns"], t["show"]
        at = {c: i for i, c in enumerate(cols)}
        entries, per = [], defaultdict(int)
        for e in t["entries"]:
            per[e["outcome"]] += 1
            entries.append({
                "outcome": e["outcome"], "row": e["row"], "accepted_row": e["accepted_row"],
                "cells": [show(c, e["values"].get(c)) for c in cols],
                "was": {str(at[c]): show(c, w) or "(blank)" for c, w in e["was"].items()
                        if c in at},
                "note": e["note"], "gone": e["gone"],
            })
        out["sheets"].append({"sheet": s.get("sheet"), "columns": cols, "entries": entries,
                              "counts": dict(per), "message": None})
    return out


# ── the CSV ─────────────────────────────────────────────────────────────────

def value_lines(row: dict, fields: Optional[dict] = None) -> list[tuple]:
    """(field, accepted value, this file's value, difference) — one per value
    that differs. A changed or restated row lists every value; a new or
    disappeared row its amounts."""
    fields = fields or {}
    o, money = row.get("outcome"), row.get("money") or {}
    out: list[tuple] = []
    if o in (vd.NEW, vd.DISAPPEARED):
        for c in _amounts(list(money), fields):
            m = money[c]
            out.append((c, m.get("was"), m.get("now"), m.get("change")))
    else:
        for c in sorted(money, key=lambda c: vd._is_rate(c, fields.get(c))):
            m = money[c]
            out.append((c, m.get("was"), m.get("now"), m.get("change")))
        for c, ch in (row.get("changes") or {}).items():
            was, now = ch.get("was"), ch.get("now")
            out.append((c, "(blank)" if was in (None, "") else was,
                        "(blank)" if now in (None, "") else now, None))
    return out or [(None, None, None, None)]


def line_values(row: dict, line: tuple) -> list:
    """One CSV line (LINE_COLUMNS) for one value of one row."""
    outcome = vd.OUTCOME_WORDS.get(row.get("outcome"), row.get("outcome"))
    field, was, now, diff = line
    eff = row.get("effective_date")
    return [outcome, row.get("policy"), row.get("insured"), row.get("row"),
            row.get("accepted_row"), field, was, now, diff, row.get("detail") or "",
            row.get("type"), _when(eff) or eff]


def sorted_rows(result: dict) -> list[dict]:
    """New, Changed, Disappeared, Restated, then Unchanged (the task's order)
    — and by row within each."""
    order = {o: i for i, o in enumerate(vd.OUTCOMES)}
    return sorted(result.get("rows") or [], key=lambda r: (
        order.get(r.get("outcome"), 99), str(r.get("sheet") or ""),
        r.get("row") or 0, r.get("accepted_row") or 0))


def to_csv(result: dict) -> str:
    """The row-by-row list as CSV: one line per value that differs (every
    line, no cap), after a line with the plain-words summary."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([TITLE, (result.get("summary") or {}).get("plain") or ""])
    w.writerow(list(LINE_COLUMNS))
    fields = money_fields(result)
    for row in sorted_rows(result):
        for line in value_lines(row, fields):
            vals = line_values(row, line)
            w.writerow(["" if v is None else v.isoformat() if isinstance(v, date) else v
                        for v in vals])
    return buf.getvalue()


# ── the sheet older outputs carry ───────────────────────────────────────────

def _attr(element: str, name: str) -> Optional[str]:
    m = re.search(r"(?<![\w:])" + re.escape(name) + r'="([^"]*)"', element)
    return m.group(1) if m else None


def _targets(rels_xml: str, base: str) -> dict[str, str]:
    """{relationship id: part path in the zip} from a .rels part; `base` is
    the folder its relative targets start from."""
    out = {}
    for rel in re.findall(r"<Relationship\b[^>]*>", rels_xml):
        rid, tgt = _attr(rel, "Id"), _attr(rel, "Target")
        if rid and tgt and _attr(rel, "TargetMode") != "External":
            out[rid] = (tgt.lstrip("/") if tgt.startswith("/")
                        else posixpath.normpath(posixpath.join(base, tgt)))
    return out


def _rels_of(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", name + ".rels")


def _first_cell(z: zipfile.ZipFile, part: str, names: set) -> Optional[str]:
    """The text in A1 of a worksheet part — inline or shared string."""
    xml = z.read(part).decode("utf-8", "ignore")
    m = re.search(r'<c\b[^>]*\br="A1"[^>]*>(.*?)</c>', xml, re.S)
    if not m:
        return None
    cell = m.group(0)
    if 't="s"' in cell.split(">", 1)[0]:
        i = re.search(r"<v>(\d+)</v>", cell)
        if not i or "xl/sharedStrings.xml" not in names:
            return None
        items = re.findall(r"<si>(.*?)</si>",
                           z.read("xl/sharedStrings.xml").decode("utf-8", "ignore"), re.S)
        k = int(i.group(1))
        return "".join(re.findall(r"<t[^>]*>([^<]*)</t>", items[k])) if k < len(items) else None
    return "".join(re.findall(r"<t[^>]*>([^<]*)</t>", m.group(1))) or None


def has_sheet(xlsx_bytes: bytes) -> bool:
    """Whether a saved workbook carries a sheet this module made (outputs
    before 19 Oct 2026) — read from the zip, without loading the workbook."""
    try:
        return bool(_ours(zipfile.ZipFile(io.BytesIO(xlsx_bytes)))[0])
    except Exception:  # noqa: BLE001 — not a workbook: nothing of ours in it
        return False


def _ours(z: zipfile.ZipFile) -> tuple[list[tuple[int, str, str, str]], str, dict]:
    """([(position, <sheet> element, relationship id, part)] of the sheets
    this module made, workbook.xml, {rel id: part})."""
    wb = z.read("xl/workbook.xml").decode("utf-8")
    if f'name="{SHEET_NAME}' not in wb:
        return [], wb, {}
    target = _targets(z.read("xl/_rels/workbook.xml.rels").decode("utf-8"), "xl")
    names = set(z.namelist())
    found = []
    for i, el in enumerate(re.findall(r"<sheet\b[^>]*>", re.search(
            r"<sheets\b[^>]*>(.*?)</sheets>", wb, re.S).group(1))):
        rid = _attr(el, "r:id")
        part = target.get(rid)
        if ((_attr(el, "name") or "").startswith(SHEET_NAME) and part in names
                and _first_cell(z, part, names) == TITLE):
            found.append((i, el, rid, part))
    return found, wb, target


def strip_sheet(xlsx_bytes: bytes) -> bytes:
    """The workbook without the "Previous submission" sheet an output made
    before 19 Oct 2026 carries — taken out of the zip directly, so every
    other part (the bordereau, its notes, the totals' saved results) is
    served byte for byte as stored. A template's own sheet of that name is
    kept (it is ours only with our title in A1). Never raises: anything odd
    returns the bytes unchanged."""
    try:
        z = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
        ours, wb, _target = _ours(z)
        if not ours:
            return xlsx_bytes
        names = set(z.namelist())
        drop_parts, drop_rids = set(), set()
        for _i, _el, rid, part in ours:
            drop_rids.add(rid)
            drop_parts.add(part)
            rels = _rels_of(part)
            if rels in names:           # its notes and their drawing go with it
                drop_parts.add(rels)
                drop_parts.update(_targets(z.read(rels).decode("utf-8"), posixpath.dirname(part))
                                  .values())
        gone = sorted(i for i, *_ in ours)
        n_left = len(re.findall(r"<sheet\b", wb)) - len(gone)
        if n_left < 1:
            return xlsx_bytes
        for _i, el, *_ in ours:
            wb = wb.replace(el, "", 1)

        def shift(i: int) -> int:
            return i - sum(1 for g in gone if g < i)

        dropped = [f"'{_attr(el, 'name')}'!" for _i, el, *_ in ours]

        def name_fix(m):
            el = m.group(0)
            local = _attr(el, "localSheetId")
            if local is not None and int(local) in gone:
                return ""
            if any(d in el for d in dropped):       # a name pointing into the sheet
                return ""
            if local is not None:
                el = el.replace(f'localSheetId="{local}"', f'localSheetId="{shift(int(local))}"', 1)
            return el
        wb = re.sub(r"<definedName\b[^>]*>.*?</definedName>", name_fix, wb, flags=re.S)
        wb = re.sub(r"<definedNames>\s*</definedNames>|<definedNames/>", "", wb)

        def view_fix(m):
            el = m.group(0)
            for a in ("activeTab", "firstSheet"):
                v = _attr(el, a)
                if v is not None:
                    i = int(v)
                    new = 0 if i in gone else min(shift(i), n_left - 1)
                    el = el.replace(f'{a}="{v}"', f'{a}="{new}"', 1)
            return el
        wb = re.sub(r"<workbookView\b[^>]*>", view_fix, wb)

        # The calculation chain names sheets by id; Excel rebuilds it when it
        # is missing, so it goes rather than being edited.
        calc = "xl/calcChain.xml"
        wb_rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        for rel in re.findall(r"<Relationship\b[^>]*>", wb_rels):
            if _attr(rel, "Id") in drop_rids or (calc in names and (_attr(rel, "Target") or "")
                                                 .endswith("calcChain.xml")):
                wb_rels = wb_rels.replace(rel, "", 1)
        if calc in names:
            drop_parts.add(calc)

        types = z.read("[Content_Types].xml").decode("utf-8")
        for o in re.findall(r"<Override\b[^>]*>", types):
            if (_attr(o, "PartName") or "").lstrip("/") in drop_parts:
                types = types.replace(o, "", 1)

        changed = {"xl/workbook.xml": wb, "xl/_rels/workbook.xml.rels": wb_rels,
                   "[Content_Types].xml": types}
        if "docProps/custom.xml" in names:
            custom = z.read("docProps/custom.xml").decode("utf-8")
            changed["docProps/custom.xml"] = re.sub(
                r'<property\b[^>]*\bname="' + re.escape(_STAMP) + r'"[^>]*>.*?</property>', "",
                custom, flags=re.S)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for item in z.infolist():
                if item.filename in drop_parts:
                    continue
                body = changed.get(item.filename)
                out.writestr(item, body.encode("utf-8") if body is not None
                             else z.read(item.filename))
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — a download never fails over this sheet
        return xlsx_bytes
