"""
financial_figures.py
────────────────────
Financial Summary (Phase 2, task 35): the money on each row of a processed
bordereau, and how sure we are of it. Pure — no database; financial_summary.py
feeds it the files.

THE MAPPING IS THE CARRIER'S, AS IT IS (decision of 6 Nov 2026)
  Which figure a column holds comes from ONE place: the column's data-model
  field (`canonical_field`) on its output template, as the carrier set it in
  Bordereau Setup / the output template page. Nothing here corrects, overrides
  or guesses it — a wrong tag gives a wrong figure until the carrier fixes the
  tag. The template a file is read with is the latest one the carrier set up
  for it (financial_summary.latest_of), so a mapping fix reaches old files too.

READ BY HEADING, NEVER BY POSITION
  The tagged column is found in the file by its HEADING
  (output_template_fields.header_of, then its name), not by the template's
  `column_index`: the writer drops empty columns, so a 145-column template can
  give a 62-column file, and older files predate later template edits. The
  heading only says WHERE the tagged column sits; the tag says what it is. A
  tagged column whose heading is not in the file was not written, i.e. empty.

SEVERAL COLUMNS, ONE FIGURE
  Columns of one sheet tagged with the same field must agree (else the value
  needs review) — unless the carrier has ticked "Add together" (`add_together`)
  on every one of them, for a money figure: then their values are added, and a
  part that is missing on a row makes that row's figure need review rather than
  a smaller total. Ticked on some and not others: needs review, so a total and
  its parts are never counted twice. Rates are never added together.

FIVE LABELS, ONE PER VALUE
  reported      the bordereau carries it — in one column, in several that
                agree, or as the sum of columns the carrier marked Add together
  calculated    worked out from reported values on the same row by an approved
                formula (below); `how` shows the working
  needs_review  the columns tagged with this field disagree, an Add together
                part is missing, or a percentage's convention could not be
                verified
  not_reported  the bordereau does not carry it
  (not_available is the summary's label for a figure whose inputs do not exist
  at all — Net Written Premium; see financial_summary.py.)

THE RULES (approved by the user, 5 Nov 2026)
  * Gross premium and Total gross written premium (100%) are separate values;
    neither is derived from the other (no share % exists to do it with).
  * Commission amount = gross premium × commission % ÷ 100, and
    commission %    = commission amount ÷ gross premium × 100 — only when the
    missing one is not reported and both inputs are. Brokerage the same, kept
    apart from commission.
  * Bordereau net premium, earned premium, fees and taxes: reported only.
    Nothing is deducted from anything.
  * Cancellation and return premium rows (APPROVED_NEGATIVE) count as negative
    in every money amount, whatever sign the file used; the amount as sent is
    kept beside it. Percentages keep their value. Every other row keeps the
    sign it was sent with.
  * A percentage is converted from a fraction (0.235 → 23.5) only when the
    template's own rows prove it is stored that way (see `convention`).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import ceded_engine as ce
from rule_windows import as_date

# ── what is read ────────────────────────────────────────────────────────────
# key → (data-model field, kind). The data model's own keys, as the output
# templates carry them.
FIELDS: dict[str, tuple[str, str]] = {
    "policy":            ("policy_number", "text"),
    "gross_premium":     ("premium_transaction_gross_premium_amount", "money"),
    "total_gwp":         ("premium_transaction_total_gross_written_premium_amount", "money"),
    "commission_amount": ("premium_transaction_commission_amount", "money"),
    "commission_pct":    ("premium_transaction_commission_percent", "percent"),
    "brokerage_amount":  ("premium_transaction_brokerage_amount", "money"),
    "brokerage_pct":     ("premium_transaction_brokerage_percent", "percent"),
    "fees":              ("premium_transaction_fee_total_amount", "money"),
    "taxes":             ("premium_transaction_tax_total_amount", "money"),
    "net_premium":       ("premium_transaction_net_premium_amount", "money"),
    "earned_premium":    ("premium_transaction_earned_premium_amount", "money"),
    "transaction_type":  ("premium_transaction_type", "text"),
    "cover_start":       ("policy_effective_date", "date"),
    "cover_end":         ("policy_expiration_date", "date"),
    "currency":          ("premium_transaction_original_currency", "text"),
}
_KEY_OF = {f: k for k, (f, _kind) in FIELDS.items()}
KIND = {k: kind for k, (_f, kind) in FIELDS.items()}
MONEY = tuple(k for k, kind in KIND.items() if kind == "money")
PERCENT = tuple(k for k, kind in KIND.items() if kind == "percent")
# (amount, percent) pairs the two identities connect.
RATES = {"commission": ("commission_amount", "commission_pct"),
         "brokerage": ("brokerage_amount", "brokerage_pct")}

REPORTED, CALCULATED, REVIEW, MISSING = "reported", "calculated", "needs_review", "not_reported"

# The carrier's "Add together" tick on a template column (output template page).
ADD_KEY = "add_together"


def field_of(col: dict) -> Optional[str]:
    """The data-model field a template column holds: its canonical_field, as
    the carrier mapped it. The only source."""
    return col.get("canonical_field") or None


def adds_together(col: dict) -> bool:
    return col.get(ADD_KEY) is True

# Two money values are the same when they agree to the cent.
_CENT = 0.005


# ── transaction types ───────────────────────────────────────────────────────
def code_of(value: Any) -> str:
    """A transaction code as compared: capitals, single spaces, one kind of
    dash. "Endorsement – Return Premium" and "ENDORSEMENT-RETURN PREMIUM" are
    the same code."""
    s = str(value or "").upper().replace("–", "-").replace("—", "-")
    s = re.sub(r"\s*-\s*", " - ", s)
    return " ".join(s.split())


def _txn_map() -> dict[str, str]:
    from ingester import _TXN_TYPE_MAP
    return _TXN_TYPE_MAP


def approved_negative() -> frozenset[str]:
    """The codes that make a row a cancellation or a return of premium — the
    list the user approved on 5 Nov 2026: CN, Cancellation, the code map's own
    flat-cancellation codes, and Endorsement - Return Premium. Other codes the
    map files under "cancellation" (C, CANC, CANCEL) were not on that list and
    keep the sign they were sent with."""
    flat = {code_of(k) for k, v in _txn_map().items() if v == "flat_cancellation"}
    return frozenset({"CN", "CANCELLATION", "ENDORSEMENT - RETURN PREMIUM"} | flat)


RETURN_PREMIUM = "ENDORSEMENT - RETURN PREMIUM"
KNOWN_CLASSES = frozenset({"new", "renewal", "endorsement", "cancellation", "reinstatement",
                           "installment", "adjustment", "flat_cancellation", "audit"})


def classify(value: Any) -> dict:
    """{"sent", "class", "negative"} for one transaction type as written.

    The class is the ingester's own code map (NB → new, CN → cancellation …),
    so this screen and the data model read a code the same way; a code the map
    does not know (ON, OF) is "unclassified" and shown as sent."""
    if value in (None, ""):
        return {"sent": None, "class": None, "negative": False}
    from ingester import _normalise_enum
    code = code_of(value)
    if code == RETURN_PREMIUM:
        cls = "return_premium"
    else:
        mapped = _normalise_enum("premium_transaction", "premium_transaction_type", code)
        cls = mapped if mapped in KNOWN_CLASSES else "unclassified"
    return {"sent": " ".join(str(value).split()), "class": cls,
            "negative": code in approved_negative()}


# ── reading one file ────────────────────────────────────────────────────────
@dataclass
class FileRows:
    """What one processed file yields: its policy rows, or why it cannot be read."""
    rows: list[dict] = field(default_factory=list)
    problem: Optional[str] = None
    # Tagged columns whose heading is not in the file (written empty, or the
    # template has changed since). Counted, never guessed at.
    absent: int = 0
    # % convention evidence, per rate: {"whole": n, "fraction": n, "above_one": bool, "nonzero": n}
    evidence: dict = field(default_factory=dict)


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().lower()


def _header_text(col: dict) -> list[str]:
    from output_template_fields import header_of
    names = [header_of(col), col.get("column_name")]
    return [n for n in dict.fromkeys(_norm(n) for n in names if n) if n]


def _is_reference(sh: dict) -> bool:
    from exporter import is_reference_sheet
    return is_reference_sheet(sh)


def _tables(data: bytes, filename: str, sheets: list[dict]) -> tuple[dict[str, list[list]], Optional[str]]:
    """{template sheet name: the sheet's cells} for the sheets the file has."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        table = list(csv.reader(io.StringIO(text)))
        return ({sheets[0].get("sheet_name"): table} if sheets else {}), None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        return {}, f"the processed file could not be opened ({e})"
    try:
        by_name = {ws.title: ws for ws in wb.worksheets}
        by_norm = {_norm(t): ws for t, ws in by_name.items()}
        out = {}
        for sh in sheets:
            want = sh.get("sheet_name")
            ws = by_name.get(want) or by_norm.get(_norm(want or ""))
            if ws is not None:
                out[want] = [list(r) for r in ws.iter_rows(values_only=True)]
        return out, None
    finally:
        wb.close()


def _headings(table: list[list], sh: dict) -> dict[str, int]:
    """{normalised heading: 0-based column} from the template's header row,
    else the first row (the plain writer always heads row 1). First occurrence
    wins, as the highlighter does."""
    for idx in dict.fromkeys([int(sh.get("header_row") or 0), 0]):
        if idx < len(table):
            found: dict[str, int] = {}
            for i, v in enumerate(table[idx] or []):
                if v is not None and str(v).strip():
                    found.setdefault(_norm(v), i)
            if found:
                return found
    return {}


def _parse(kind: str, raw: Any) -> tuple[Any, bool]:
    """(value, readable) for one non-empty cell."""
    if kind in ("money", "percent"):
        v = ce.num(raw)
        return v, v is not None
    if kind == "date":
        d = as_date(raw)
        return (d.isoformat() if d else " ".join(str(raw).split())), d is not None
    return " ".join(str(raw).split()), True


def _same(kind: str, a: Any, b: Any) -> bool:
    if kind in ("money", "percent"):
        return abs(a - b) < _CENT
    return str(a).strip().upper() == str(b).strip().upper()


def _sent(raw: list, pos: Optional[int]) -> bool:
    return pos is not None and pos < len(raw) and raw[pos] not in (None, "") and bool(str(raw[pos]).strip())


def _cell(kind: str, found: list[tuple[Any, str]]) -> Optional[dict]:
    """One field on one row, from every column tagged with it that has a value.

    found: [(raw value, heading)]. Several columns must agree."""
    if not found:
        return None
    parsed = [(*_parse(kind, raw), raw, head) for raw, head in found]
    cols = [head for *_x, head in parsed]
    if kind in ("money", "percent"):
        bad = next((head for _v, ok, _raw, head in parsed if not ok), None)
        if bad:
            return {"state": REVIEW, "cols": cols, "why": f"“{bad}” is not a number"}
    first = parsed[0][0]
    if any(not _same(kind, first, v) for v, *_x in parsed[1:]):
        distinct, shown = [], []
        for v, _ok, raw, head in parsed:
            if not any(_same(kind, v, d) for d in distinct):
                distinct.append(v)
                shown.append(f"{head} = {raw}")
        return {"state": REVIEW, "cols": cols,
                "why": f"its columns disagree ({', '.join(shown[:3])})"}
    raw0 = parsed[0][2]
    out = {"state": "ok", "value": first, "cols": cols}
    if kind == "percent" and isinstance(raw0, str) and "%" in raw0:
        out["written_pct"] = True        # "23.5%" is a whole percentage, whatever the template
    return out


def _quoted(heads: list[str]) -> str:
    return ", ".join(f"“{h}”" for h in heads)


def _added(members: list[tuple[Optional[int], str, bool]], raw: list) -> Optional[dict]:
    """One money figure on one row from several columns tagged with it, where
    at least one is marked Add together.

    members: every template column of the sheet tagged with the field —
    (file column or None when its heading is not in the file, heading,
    marked Add together)."""
    heads = [h for _p, h, _a in members]
    sent = [h for p, h, _a in members if _sent(raw, p)]
    if not sent:
        return None
    if not all(a for _p, _h, a in members):
        return {"state": REVIEW, "cols": heads,
                "why": (f"{_quoted([h for _p, h, a in members if a])} marked Add together, "
                        f"{_quoted([h for _p, h, a in members if not a])} not — "
                        "tick all of them or none")}
    parts, missing = [], []
    for p, h, _a in members:
        if not _sent(raw, p):
            missing.append(h)
            continue
        v, ok = _parse("money", raw[p])
        if not ok:
            return {"state": REVIEW, "cols": heads, "why": f"“{h}” is not a number"}
        parts.append((h, float(v)))
    if missing:
        return {"state": REVIEW, "cols": heads,
                "why": f"Add together: {_quoted(missing)} not sent on this row"}
    return {"state": "ok", "value": sum(v for _h, v in parts), "cols": heads, "parts": parts}


def read_file(structure: dict, data: Optional[bytes], filename: str) -> FileRows:
    """Every policy row of one processed file, field by field, read by heading.

    A row with no policy number is a total or a note, not a policy, and is left
    out — as Policy Search does."""
    if not data:
        return FileRows(problem="the processed file is no longer stored")
    sheets = [sh for sh in (structure or {}).get("sheets") or []
              if not _is_reference(sh)
              and any((field_of(c) in _KEY_OF) for c in sh.get("columns") or [])]
    if not sheets:
        return FileRows(problem="its output template tags none of the policy or premium columns")
    tables, problem = _tables(data, filename, sheets)
    if problem:
        return FileRows(problem=problem)
    out = FileRows()
    readable = 0
    for sh in sheets:
        table = tables.get(sh.get("sheet_name"))
        if table is None:
            continue
        heads = _headings(table, sh)
        if not heads:
            continue
        # key → every column of the sheet tagged with it:
        # (file column or None when not written, heading, marked Add together)
        members: dict[str, list[tuple[Optional[int], str, bool]]] = {}
        # The same, for the columns switched on in the template — the only
        # ones an Add together group is made of (a switched-off column is never
        # written, so it would read as a missing part on every row).
        live: dict[str, list[tuple[Optional[int], str, bool]]] = {}
        for c in sh.get("columns") or []:
            key = _KEY_OF.get(field_of(c))
            if key is None:
                continue
            pos = next((heads[n] for n in _header_text(c) if n in heads), None)
            if pos is None:
                out.absent += 1
            m = (pos, str(c.get("column_name") or "").strip(), adds_together(c))
            members.setdefault(key, []).append(m)
            if c.get("active") is not False:
                live.setdefault(key, []).append(m)
        if not any(p is not None for p, _h, _a in members.get("policy", [])):
            continue
        readable += 1
        added = {k for k, m in live.items()
                 if KIND[k] == "money" and len(m) > 1 and any(a for _p, _h, a in m)}
        start = int(sh.get("data_start_row") or 1)
        for i, raw in enumerate(table[start:], start=start + 1):
            if not raw or all(v in (None, "") for v in raw):
                continue
            cells = {}
            for key, m in members.items():
                c = (_added(live[key], raw) if key in added else
                     _cell(KIND[key], [(raw[p], h) for p, h, _a in m if _sent(raw, p)]))
                if c is not None:
                    cells[key] = c
            pol = cells.get("policy")
            if not pol or pol["state"] != "ok":
                continue
            cells["_sheet"], cells["_row"] = sh.get("sheet_name"), i
            out.rows.append(cells)
    if not readable:
        out.problem = "the file's column headings do not match its output template"
    out.evidence = _evidence(out.rows)
    return out


def marked(structure: dict) -> dict[str, bool]:
    """{figure (FIELDS key): True when a column for it is switched on, False
    when its only columns are switched off} on the sheets read_file reads — so
    a figure no row reports can be told apart as "no column for it", "a column
    switched off in the template" (never written) or "a column left empty".
    A figure with no column at all is absent."""
    out: dict[str, bool] = {}
    for sh in (structure or {}).get("sheets") or []:
        if _is_reference(sh):
            continue
        for c in sh.get("columns") or []:
            key = _KEY_OF.get(field_of(c))
            if key is not None:
                out[key] = out.get(key, False) or c.get("active") is not False
    return out


# ── percentage convention ───────────────────────────────────────────────────
def _evidence(rows: list[dict]) -> dict:
    """How each rate is written in this file, from rows that carry the amount,
    the rate and the gross premium together: amount = premium × rate ÷ 100
    (whole) or amount = premium × rate (fraction)."""
    ev = {}
    for name, (amt_k, pct_k) in RATES.items():
        e = {"whole": 0, "fraction": 0, "above_one": False, "nonzero": 0}
        for r in rows:
            p = _ok(r.get(pct_k))
            if p is None:
                continue
            if abs(p) > 1:
                e["above_one"] = True
            if p != 0:
                e["nonzero"] += 1
            a, g = _ok(r.get(amt_k)), _ok(r.get("gross_premium"))
            if a is None or not g or p == 0:
                continue
            tol = max(0.01, abs(a) * 0.005)
            whole = abs(abs(a) - abs(g) * abs(p) / 100) <= tol
            frac = abs(abs(a) - abs(g) * abs(p)) <= tol
            if whole != frac:
                e["whole" if whole else "fraction"] += 1
        ev[name] = e
    return ev


def pool(evidences: list[dict]) -> dict:
    """One template's evidence over all of its files."""
    out: dict = {}
    for ev in evidences:
        for name, e in (ev or {}).items():
            o = out.setdefault(name, {"whole": 0, "fraction": 0, "above_one": False, "nonzero": 0})
            o["whole"] += e["whole"]
            o["fraction"] += e["fraction"]
            o["above_one"] = o["above_one"] or e["above_one"]
            o["nonzero"] += e["nonzero"]
    return out


def convention(e: Optional[dict]) -> tuple[str, str]:
    """("whole" | "fraction" | "unknown", why) for one rate of one template."""
    e = e or {}
    w, f = e.get("whole", 0), e.get("fraction", 0)
    if w > f:
        return "whole", f"checked against amount ÷ premium on {w} row{'s' if w != 1 else ''}"
    if f > w:
        return "fraction", f"stored as a fraction — checked against amount ÷ premium on {f} row{'s' if f != 1 else ''}"
    if e.get("above_one"):
        return "whole", "the column holds values above 1, which a fraction of the premium cannot"
    if not e.get("nonzero"):
        return "whole", "every value is 0"
    return "unknown", "whether 0.25 means 25% or 0.25% could not be checked: no row carries the amount, the rate and the premium together"


def _ok(cell: Optional[dict]) -> Optional[float]:
    if cell and cell.get("state") == "ok" and isinstance(cell.get("value"), (int, float)):
        return float(cell["value"])
    return None


# ── one row's figures ───────────────────────────────────────────────────────
def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def figures(cells: dict, conventions: dict[str, tuple[str, str]]) -> dict:
    """Every figure of one row: {key: {"value", "source", "status", "how"}},
    plus "transaction" ({"sent", "class", "negative"}).

    `value` is what the summary adds up; `source` is what the bordereau said.
    They differ only on a cancellation or return of premium, which counts as
    negative in every money amount."""
    t = cells.get("transaction_type")
    if t and t["state"] == "ok":
        txn = classify(t["value"])
    else:
        txn = {"sent": None, "class": None, "negative": False}
        if t:
            txn["review"] = t.get("why")
    neg = txn["negative"]
    out: dict = {"transaction": txn}

    for k in MONEY:
        c = cells.get(k)
        if c is None:
            out[k] = {"value": None, "source": None, "status": MISSING, "how": None}
        elif c["state"] != "ok":
            out[k] = {"value": None, "source": None, "status": REVIEW, "how": c.get("why")}
        elif c.get("parts"):
            # Add together: each part as sent, then (on a cancellation or a
            # return of premium) each part made negative before adding.
            v = float(c["value"])
            sum_txt = " + ".join(f"{h} {_fmt(p)}" for h, p in c["parts"]) + f" = {_fmt(v)}"
            if neg and any(p != 0 for _h, p in c["parts"]):
                counted = -sum(abs(p) for _h, p in c["parts"])
                out[k] = {"value": counted, "source": v, "status": REPORTED,
                          "how": f"added together: {sum_txt}; on a {txn['sent']} row, counted as {_fmt(counted)}"}
            else:
                out[k] = {"value": v, "source": v, "status": REPORTED, "how": f"added together: {sum_txt}"}
        else:
            v = float(c["value"])
            if neg and v != 0:
                out[k] = {"value": -abs(v), "source": v, "status": REPORTED,
                          "how": f"sent as {_fmt(v)} on a {txn['sent']} row, counted as {_fmt(-abs(v))}"}
            else:
                out[k] = {"value": v, "source": v, "status": REPORTED, "how": None}

    for name, (amt_k, pct_k) in RATES.items():
        c = cells.get(pct_k)
        conv, why = conventions.get(name) or ("unknown", "not checked")
        if c is None:
            out[pct_k] = {"value": None, "source": None, "status": MISSING, "how": None}
        elif c["state"] != "ok":
            out[pct_k] = {"value": None, "source": None, "status": REVIEW, "how": c.get("why")}
        else:
            v = float(c["value"])
            if c.get("written_pct") or v == 0 or conv == "whole":
                out[pct_k] = {"value": v, "source": v, "status": REPORTED, "how": None}
            elif conv == "fraction":
                out[pct_k] = {"value": v * 100, "source": v, "status": REPORTED,
                              "how": f"written as {v:g}, a fraction ({why}) — {v * 100:g}%"}
            else:
                out[pct_k] = {"value": None, "source": v, "status": REVIEW, "how": why}

        # The two identities, only to fill a gap and only from reported inputs.
        amt, pct, gross = out[amt_k], out[pct_k], out["gross_premium"]
        if amt["status"] == MISSING and pct["status"] == REPORTED and gross["status"] == REPORTED:
            v = gross["value"] * pct["value"] / 100
            out[amt_k] = {"value": v, "source": None, "status": CALCULATED,
                          "how": f"gross premium {_fmt(gross['value'])} × {pct['value']:g}% ÷ 100 = {_fmt(v)}"}
        elif amt["status"] == MISSING and REVIEW in (pct["status"], gross["status"]) \
                and MISSING not in (pct["status"], gross["status"]):
            out[amt_k] = {"value": None, "source": None, "status": REVIEW,
                          "how": "would be gross premium × rate, but an input needs review"}
        if pct["status"] == MISSING and amt["status"] == REPORTED \
                and gross["status"] == REPORTED and gross["value"]:
            v = abs(amt["value"]) / abs(gross["value"]) * 100
            out[pct_k] = {"value": v, "source": None, "status": CALCULATED,
                          "how": f"{_fmt(abs(amt['value']))} ÷ gross premium {_fmt(abs(gross['value']))} × 100 = {v:.2f}%"}

    for k in ("cover_start", "cover_end", "currency"):
        c = cells.get(k)
        if c is None:
            out[k] = {"value": None, "status": MISSING}
        elif c["state"] != "ok":
            out[k] = {"value": None, "status": REVIEW, "how": c.get("why")}
        else:
            v = c["value"]
            out[k] = {"value": str(v).upper() if k == "currency" else v, "status": REPORTED}
    out["policy"] = cells["policy"]["value"]
    return out


# ── cache key ───────────────────────────────────────────────────────────────
def template_stamp(structure: dict) -> str:
    """What in a template decides which heading is which field — the tags, the
    headings and the Add together ticks."""
    shape = [(sh.get("sheet_name"), sh.get("data_start_row"), sh.get("header_row"),
              sh.get("sheet_role"), sh.get("rule_generatable"),
              [(field_of(c), c.get("column_name"), c.get("display_name"), adds_together(c),
                c.get("active") is not False)
               for c in sh.get("columns") or []])
             for sh in (structure or {}).get("sheets") or []]
    return hashlib.sha256(json.dumps(shape, default=str).encode()).hexdigest()[:16]
