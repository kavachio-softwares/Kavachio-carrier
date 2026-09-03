"""Serialize mapped output rows into CSV, XML or JSON.

Excel output is still produced by ``exporter.generate_workbook`` /
``direct_render.render_output`` (they preserve the sample workbook's styling).
This module covers the non-xlsx formats, working from a format-neutral shape:

    sheets = [
        {"sheet_name": str, "columns": [col_name, ...], "rows": [{col: val}, ...]},
        ...
    ]

Multi-sheet CSV can't live in one flat file, so several sheets are bundled as a
ZIP of per-sheet ``.csv`` files (a single-sheet output stays a plain ``.csv``).
JSON and XML represent every sheet natively.

The three call sites (Lane A export, Lane B direct render, and the download
endpoints) share one notion of "what extension / MIME does this format use",
kept here so they never drift.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from xml.sax.saxutils import escape

# The formats an output template may be configured for. Anything else falls
# back to xlsx (the historical default).
SUPPORTED_FORMATS = ("xlsx", "csv", "xml", "json")

_CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".xml": "application/xml",
    ".json": "application/json",
    ".zip": "application/zip",
}


def normalize_format(fmt: str | None) -> str:
    """Coerce a stored/blank format string to one of SUPPORTED_FORMATS."""
    f = (fmt or "xlsx").strip().lower()
    return f if f in SUPPORTED_FORMATS else "xlsx"


def output_extension(fmt: str, n_sheets: int) -> str:
    """File extension for a format. Multi-sheet CSV becomes a .zip bundle."""
    fmt = normalize_format(fmt)
    if fmt == "csv":
        return ".zip" if n_sheets > 1 else ".csv"
    return {"xlsx": ".xlsx", "xml": ".xml", "json": ".json"}.get(fmt, ".xlsx")


def content_type_for_filename(filename: str | None) -> str:
    """MIME type inferred from a filename's extension (defaults to xlsx)."""
    name = (filename or "").lower()
    for ext, ct in _CONTENT_TYPES.items():
        if name.endswith(ext):
            return ct
    return _CONTENT_TYPES[".xlsx"]


def safe_filename(name: str, fallback: str = "export") -> str:
    """A download name that survives being mailed, zipped and re-opened.

    Template names read well on screen — "Carrier 1 — Programme A — Bridge
    Brokers" — and badly as filenames: the em-dash is non-ASCII, and a file
    called ``Carrier_1_—_Programme_A.xlsx`` renders differently (or not at all)
    depending on where it lands. Dashes and slashes become plain hyphens,
    everything else unprintable is dropped, and runs of separators collapse.
    The name is still recognisable — that is the point of doing this rather than
    slugging it down to ``carrier_1_programme_a``.
    """
    import unicodedata
    # Decompose accents to their ASCII base ("Zürich" -> "Zurich") rather than
    # deleting the letter.
    text = unicodedata.normalize("NFKD", str(name or ""))
    # Non-ASCII that has no ASCII base — the em-dash in "Carrier 1 — Programme A"
    # among them — is dropped here, leaving the spaces around it to collapse
    # into the single separator below. That is deliberate: "Carrier_1_Programme_A"
    # reads better than "Carrier_1_-_Programme_A".
    text = text.encode("ascii", "ignore").decode("ascii")
    out = []
    for ch in text:
        if ch.isalnum() or ch in "-_.()[]":
            out.append(ch)
        elif ch in " \t/\\":
            out.append("_")
    cleaned = "".join(out)
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("_-. ")
    return cleaned or fallback


def ensure_extension(name: str, ext: str) -> str:
    """Give ``name`` the extension ``ext``, replacing any known data extension."""
    lower = name.lower()
    known = (".xlsx", ".xls", ".csv", ".xml", ".json", ".zip")
    for k in known:
        if lower.endswith(k):
            return name[: -len(k)] + ext
    return name + ext


# --- value coercion ---------------------------------------------------------

def _is_nan(val: Any) -> bool:
    # NaN is the only value not equal to itself; guard the isinstance for speed.
    return isinstance(val, float) and val != val


def _json_value(val: Any) -> Any:
    if val is None or _is_nan(val):
        return None
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Decimal):
        f = float(val)
        return int(val) if f.is_integer() else f
    if isinstance(val, (str, int, float, bool)):
        return val
    return str(val)


def _text_value(val: Any) -> str:
    if val is None or _is_nan(val):
        return ""
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return str(val)


# --- serializers ------------------------------------------------------------

def _headers(sheet: dict) -> list[str]:
    """What the file calls each column. Falls back to the keys for any caller
    that still passes the old single-list shape."""
    return sheet.get("headers") or sheet.get("columns") or []


def _sheet_csv_bytes(sheet: dict) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    cols = sheet.get("columns") or []
    writer.writerow(_headers(sheet))
    for row in sheet.get("rows") or []:
        writer.writerow([_text_value(row.get(c)) for c in cols])
    return buf.getvalue().encode("utf-8")


def _safe_member_name(name: str, index: int, used: set[str]) -> str:
    base = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in str(name)).strip()
    base = base or f"sheet_{index + 1}"
    candidate = base
    n = 1
    while candidate.lower() in used:
        n += 1
        candidate = f"{base}_{n}"
    used.add(candidate.lower())
    return candidate


def _to_csv(sheets: list[dict]) -> bytes:
    if len(sheets) <= 1:
        return _sheet_csv_bytes(sheets[0]) if sheets else b""
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, sheet in enumerate(sheets):
            member = _safe_member_name(sheet.get("sheet_name", ""), i, used)
            zf.writestr(f"{member}.csv", _sheet_csv_bytes(sheet))
    return buf.getvalue()


def _to_json(sheets: list[dict]) -> bytes:
    obj: dict[str, list[dict]] = {}
    for sheet in sheets:
        cols = sheet.get("columns") or []
        heads = _headers(sheet)
        obj[str(sheet.get("sheet_name", ""))] = [
            {h: _json_value(row.get(c)) for c, h in zip(cols, heads)}
            for row in (sheet.get("rows") or [])
        ]
    return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")


def _to_xml(sheets: list[dict]) -> bytes:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<workbook>"]
    for sheet in sheets:
        cols = sheet.get("columns") or []
        heads = _headers(sheet)
        parts.append(f'  <sheet name="{escape(str(sheet.get("sheet_name", "")), {chr(34): "&quot;"})}">')
        for row in (sheet.get("rows") or []):
            parts.append("    <row>")
            for c, h in zip(cols, heads):
                # Column names can contain spaces/symbols, so carry them as a
                # `name` attribute rather than risk an invalid XML tag name. The
                # attribute is the column's NAME IN THE FILE, so a renamed field
                # reads the same here as it does in the xlsx and the csv.
                parts.append(
                    f'      <cell name="{escape(str(h), {chr(34): "&quot;"})}">'
                    f"{escape(_text_value(row.get(c)))}</cell>"
                )
            parts.append("    </row>")
        parts.append("  </sheet>")
    parts.append("</workbook>")
    return "\n".join(parts).encode("utf-8")


def serialize(sheets: list[dict], fmt: str) -> bytes:
    """Serialize the neutral ``sheets`` shape to bytes for a non-xlsx format.

    xlsx is intentionally NOT handled here (callers keep using the styled
    workbook writers); passing it raises so the mistake is loud.
    """
    fmt = normalize_format(fmt)
    if fmt == "csv":
        return _to_csv(sheets)
    if fmt == "json":
        return _to_json(sheets)
    if fmt == "xml":
        return _to_xml(sheets)
    raise ValueError(f"serialize() does not handle format {fmt!r} (xlsx is written elsewhere)")


# --- adapters from each lane's row model to the neutral shape ---------------

def _structure_columns(structure: dict, sheet_name: str) -> tuple[list[str], list[str]]:
    """(row keys, header texts) for one sheet.

    They used to be one list, which was fine while a column's heading and the
    key its values are stored under were always the same string. Renaming a
    field separates them: the file shows the new heading, the rows are still
    keyed by the original. Only the fields still switched on are returned, in
    the order the user arranged them — the same rule the xlsx writer follows,
    so a CSV and an XLSX of one template can never disagree.
    """
    from output_template_fields import active_columns, header_of
    for sh in structure.get("sheets", []):
        if sh.get("sheet_name") == sheet_name:
            cols = [c for c in active_columns(sh) if c.get("column_name")]
            return ([c["column_name"] for c in cols], [header_of(c) for c in cols])
    return ([], [])


def sheets_from_blocks(structure: dict, blocks: list[dict]) -> list[dict]:
    """Lane A: ``build_output_records`` output ([{sheet, records}]) → neutral shape."""
    out: list[dict] = []
    for block in blocks:
        name = block.get("sheet", "")
        keys, heads = _structure_columns(structure, name)
        out.append({
            "sheet_name": name, "columns": keys, "headers": heads,
            "rows": block.get("records") or [],
        })
    return out


def sheets_from_projected(structure: dict, projected: dict[str, list[dict]]) -> list[dict]:
    """Lane B: direct-lane ``projected`` ({sheet: [rows]}) → neutral shape.

    Driven by the template's sheet order so empty sheets still appear.
    """
    out: list[dict] = []
    for sh in structure.get("sheets", []):
        name = sh.get("sheet_name", "")
        keys, heads = _structure_columns(structure, name)
        out.append({
            "sheet_name": name, "columns": keys, "headers": heads,
            "rows": projected.get(name) or [],
        })
    return out
