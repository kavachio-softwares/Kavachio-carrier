"""
The reinsurance (ceded) bordereau — what a carrier passes to its reinsurers
for one treaty and one reporting period, worked out from the inward
bordereaux it has already processed.

WHERE THE ROWS COME FROM
  Every processed inward bordereau has a rendered file (OutputExport) written
  to an output template, and every template column is tied to a data-model
  field (`canonical_field`). So the rows are read back from that file with
  standard names — premium, sum insured, policy number, claims — whichever
  broker sent them and whatever their own headers were. This works for every
  processed bordereau, not only the few whose setup is mapped to the data
  model.

  Only the LIVE version of each submission counts (exception_tally): a file
  corrected after it was sent replaces the earlier one here too.

WHAT IS CEDED, PER TREATY TYPE
  quota_share   each premium and claim × cession %, a risk capped at the limit
                per risk when one is set
  surplus       per risk: (sum insured − retention line) ÷ sum insured, capped
                at the treaty's capacity (lines × line)
  facultative   only the policy the certificate names, × share ceded
  xol           no premium per row (the deposit premium is annual); each claim
                above a layer's retention is recovered up to the layer's limit
  stop_loss     the treaty year to date: the loss ratio (or the aggregate loss)
                against the attachment point, recovered up to the cover

  Each figure is then split by every reinsurer's share of its layer — one
  statement per reinsurer, carrying its own lines only.

Deterministic, no model call. Reading never writes; ceded_routes saves.
"""
from __future__ import annotations

import calendar
import datetime as dt
import io
import logging
import re
from typing import Any, Optional

import treaty_types as tt

log = logging.getLogger("kavachio.ceded")

# ── standard field names, with the fallbacks templates actually use ─────────
PREMIUM = ("premium_transaction_gross_premium_amount",
           "premium_transaction_total_gross_written_premium_amount")
SUM_INSURED = ("policy_sum_insured_amount", "coverage_total_insured_value")
POLICY = ("policy_number",)
INSURED = ("policyholder_legal_name",)
CURRENCY = ("premium_transaction_original_currency",
            "premium_transaction_settlement_currency",
            "policy_sum_insured_currency", "claim_currency")
TXN = ("premium_transaction_type",)
CLAIM_NO = ("claim_number",)
CLAIM_PAID = ("claim_transaction_indemnity_paid_amount",
              "claim_prior_paid_indemnity_amount")
CLAIM_RESERVE = ("claim_reserve_indemnity_amount", "reserve_amount")
CLAIM_INCURRED = ("claim_total_incurred_amount",)

LINE_COLUMNS = [
    # key, heading, kind
    ("programme", "Programme", "text"),
    ("broker", "Broker", "text"),
    ("policy", "Policy", "text"),
    ("insured", "Insured", "text"),
    ("transaction", "Transaction", "text"),
    ("claim", "Claim", "text"),
    ("layer", "Layer", "text"),
    ("gross_premium", "Gross premium", "money"),
    ("sum_insured", "Sum insured", "money"),
    ("ceded_pct", "Ceded %", "pct"),
    ("ceded_premium", "Ceded premium", "money"),
    ("ceding_commission", "Ceding commission", "money"),
    ("net_premium", "Net premium due", "money"),
    ("claims_paid", "Claims paid (ceded)", "money"),
    ("claims_outstanding", "Outstanding (ceded)", "money"),
    ("claim_incurred", "Claim incurred", "money"),
    ("recovery", "Recovery", "money"),
]
# Summed into totals and split by share. Gross premium, sum insured and a
# claim's incurred amount are facts ABOUT a line, not money ceded on it — and
# an XOL claim that reaches two layers appears on both, so adding up its
# incurred amount would count the claim twice.
MONEY_KEYS = [k for k, _h, kind in LINE_COLUMNS if kind == "money"
              and k not in ("gross_premium", "sum_insured", "claim_incurred")]


def num(v: Any) -> Optional[float]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def first(row: dict, keys: tuple) -> Any:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


# ── periods ─────────────────────────────────────────────────────────────────
def period_bounds(period: str) -> Optional[tuple[dt.date, dt.date]]:
    """'2026-03' / '2026-Q1' / '2026' → (first day, last day); None if the
    string is not one of those shapes."""
    p = (period or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})", p)
    if m:
        y, mo = int(m[1]), int(m[2])
        return dt.date(y, mo, 1), dt.date(y, mo, calendar.monthrange(y, mo)[1])
    m = re.fullmatch(r"(\d{4})-Q([1-4])", p, re.I)
    if m:
        y, q = int(m[1]), int(m[2])
        start = dt.date(y, 3 * q - 2, 1)
        end_m = 3 * q
        return start, dt.date(y, end_m, calendar.monthrange(y, end_m)[1])
    m = re.fullmatch(r"(\d{4})", p)
    if m:
        return dt.date(int(m[1]), 1, 1), dt.date(int(m[1]), 12, 31)
    return None


FREQUENCIES = ("monthly", "quarterly", "annually")


def bucket(period: str, frequency: str) -> str:
    """The statement period an inward period belongs to, for a setup sending
    `frequency` statements: '2027-02' → '2027-Q1' (quarterly) or '2027'
    (annually). A period that cannot be placed — or one already coarser than
    the setup asks for — is kept as it is."""
    b = period_bounds(period)
    if not b or frequency == "monthly":
        return period
    start = b[0]
    if frequency == "quarterly":
        if re.fullmatch(r"\d{4}", period.strip()):
            return period
        return f"{start.year}-Q{(start.month - 1) // 3 + 1}"
    return str(start.year)


def default_columns(treaty_type: Optional[str]) -> list[dict]:
    """What a statement carries unless the setup says otherwise — the columns
    that type of treaty actually fills."""
    common = ["programme", "broker", "policy", "insured", "transaction"]
    if treaty_type in ("xol", "stop_loss"):
        keys = common + ["claim", "layer", "claim_incurred", "recovery"]
    else:
        keys = common + ["gross_premium", "sum_insured", "ceded_pct",
                         "ceded_premium", "ceding_commission", "net_premium",
                         "claims_paid", "claims_outstanding"]
    heads = {k: h for k, h, _kind in LINE_COLUMNS}
    return [{"key": k, "heading": heads[k]} for k in keys]


def period_in_term(period: str, inception: Optional[dt.date],
                   expiry: Optional[dt.date]) -> bool:
    b = period_bounds(period)
    if not b or not inception or not expiry:
        return True               # unknown shape — let the carrier decide
    return b[1] >= inception and b[0] <= expiry


# ── reading a processed bordereau back ──────────────────────────────────────
def read_export_rows(template_structure: dict, data: bytes,
                     filename: str) -> tuple[list[dict], list[str]]:
    """The rows of one rendered bordereau, keyed by data-model field.

    Returns (rows, notes). Columns with no data-model field are dropped; a
    sheet the template names but the file lacks is noted, not fatal."""
    notes: list[str] = []
    rows: list[dict] = []
    sheets = (template_structure or {}).get("sheets") or []
    if not data:
        return [], ["the processed file is no longer stored"]
    name = (filename or "").lower()
    if name.endswith(".csv"):
        import csv
        text = data.decode("utf-8-sig", errors="replace")
        table = list(csv.reader(io.StringIO(text)))
        sh = sheets[0] if sheets else {}
        rows.extend(_rows_from_table(table, sh))
        return rows, notes
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        return [], [f"the processed file could not be opened ({e})"]
    by_name = {ws.title: ws for ws in wb.worksheets}
    for sh in sheets:
        cols = [c for c in sh.get("columns") or [] if c.get("canonical_field")]
        if not cols:
            continue
        ws = by_name.get(sh.get("sheet_name"))
        if ws is None:
            notes.append(f"sheet '{sh.get('sheet_name')}' is not in the file")
            continue
        table = [list(r) for r in ws.iter_rows(values_only=True)]
        rows.extend(_rows_from_table(table, sh))
    wb.close()
    return rows, notes


def _rows_from_table(table: list[list], sh: dict) -> list[dict]:
    start = int(sh.get("data_start_row") or 1)
    cols = [(int(c.get("column_index") or 0), c["canonical_field"])
            for c in sh.get("columns") or [] if c.get("canonical_field")]
    # Every named column too, by its own heading — so a column copied into a
    # reinsurer's file is that exact column, not whichever column of several
    # sharing a field happened to come first.
    named = [(int(c.get("column_index") or 0), str(c["column_name"]).strip())
             for c in sh.get("columns") or [] if str(c.get("column_name") or "").strip()]
    out = []
    for raw in table[start:]:
        if not raw or all(v in (None, "") for v in raw):
            continue
        rec: dict = {}
        for idx, field in cols:
            if idx < len(raw) and raw[idx] not in (None, ""):
                rec.setdefault(field, raw[idx])
        by_name = {name: raw[idx] for idx, name in named
                   if idx < len(raw) and raw[idx] not in (None, "")}
        if rec or by_name:
            rec["_col"] = by_name
            out.append(rec)
    return out


def coverage_of(rows: list[dict]) -> dict:
    """Which of the money fields this set of rows actually carries."""
    def has(keys):
        return any(first(r, keys) is not None for r in rows)
    return {"premium": has(PREMIUM), "sum_insured": has(SUM_INSURED),
            "claims": has(CLAIM_PAID) or has(CLAIM_RESERVE) or has(CLAIM_INCURRED)}


# ── the calculation ─────────────────────────────────────────────────────────
def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


def compute(structure: dict, rows: list[dict], *,
            ytd_rows: Optional[list[dict]] = None) -> dict:
    """Cede `rows` (each carrying `_source` context) under `structure`.

    Returns {"lines": [...], "skipped": [...], "layer_totals": {...},
             "notes": [...]} — lines are the treaty's whole share (100%),
    split by reinsurer afterwards in by_reinsurer()."""
    ttype = structure.get("treaty_type")
    terms = structure.get("terms") or {}
    layers = structure.get("layers") or []
    lines: list[dict] = []
    skipped: list[dict] = []
    notes: list[str] = []

    def base(row: dict) -> dict:
        src = row.get("_source") or {}
        return {"programme": src.get("programme"), "broker": src.get("broker"),
                "export_id": src.get("export_id"),
                "policy": _s(first(row, POLICY)), "insured": _s(first(row, INSURED)),
                "transaction": _s(first(row, TXN)),
                "currency": _s(first(row, CURRENCY)),
                # The whole inward row, for a carrier layout that passes
                # fields through (ceded_layout) — pruned before it is stored.
                "src": {**{k: v for k, v in row.items() if not k.startswith("_")},
                        "_cols": row.get("_col") or {}}}

    def skip(row: dict, why: str):
        skipped.append({**base(row), "reason": why})

    comm_pct = num(terms.get("ceding_commission_pct"))

    def proportional(row: dict, fraction: float, layer_name: str):
        prem = num(first(row, PREMIUM))
        si = num(first(row, SUM_INSURED))
        paid = num(first(row, CLAIM_PAID))
        res = num(first(row, CLAIM_RESERVE))
        if prem is None and paid is None and res is None:
            skip(row, "no premium or claim amount on the row")
            return
        ceded = prem * fraction if prem is not None else None
        comm = ceded * comm_pct / 100 if ceded is not None and comm_pct else None
        lines.append({
            **base(row), "claim": _s(first(row, CLAIM_NO)), "layer": layer_name,
            "layer_index": 0,
            "gross_premium": _r(prem), "sum_insured": _r(si),
            "ceded_pct": round(fraction * 100, 4),
            "ceded_premium": _r(ceded), "ceding_commission": _r(comm),
            "net_premium": _r((ceded or 0) - (comm or 0)) if ceded is not None else None,
            "claims_paid": _r(paid * fraction) if paid is not None else None,
            "claims_outstanding": _r(res * fraction) if res is not None else None,
            "recovery": None,
        })

    layer0 = layers[0]["name"] if layers else (tt.TREATY_TYPES.get(ttype) or {}).get(
        "default_layer", "Treaty")

    if ttype == "quota_share":
        c = (num(terms.get("cession_pct")) or 0) / 100
        limit = num(terms.get("treaty_limit"))
        for row in rows:
            frac = c
            si = num(first(row, SUM_INSURED))
            if limit and si and si * c > limit:
                frac = limit / si
            proportional(row, frac, layer0)
        if limit:
            notes.append("Risks above the limit per risk are ceded only up to it.")

    elif ttype == "surplus":
        line = num(terms.get("retention_line")) or 0
        n = num(terms.get("lines")) or 0
        cap = line * n
        for row in rows:
            si = num(first(row, SUM_INSURED))
            if not si:
                skip(row, "no sum insured — a surplus treaty cedes by sum insured")
                continue
            frac = max(0.0, min(si - line, cap)) / si if si > line else 0.0
            if frac == 0:
                skip(row, f"sum insured within the retention line ({tt.money(line)})")
                continue
            proportional(row, frac, layer0)

    elif ttype == "facultative":
        ref = str(terms.get("risk_reference") or "").strip().lower()
        frac = (num(terms.get("share_ceded_pct")) or 0) / 100
        hit = 0
        for row in rows:
            pol = str(first(row, POLICY) or "").strip().lower()
            if ref and pol == ref:
                proportional(row, frac, layer0)
                hit += 1
        if not hit:
            notes.append(f"No row for policy {terms.get('risk_reference')} in "
                         f"this period's bordereaux.")

    elif ttype == "xol":
        basis = terms.get("cover_basis")
        if basis == "per_event":
            notes.append("Claims carry no event reference, so each claim is "
                         "treated as its own event.")
        claims = [r for r in rows if _incurred(r) is not None]
        if not claims:
            notes.append("No claims data in this period's bordereaux — nothing "
                         "to recover. Premium is the annual deposit premium.")
        ordered = sorted(enumerate(layers),
                         key=lambda il: num((il[1].get("terms") or {}).get("retention")) or 0)
        for row in claims:
            inc = _incurred(row)
            for i, lay in ordered:
                lt = lay.get("terms") or {}
                ret = num(lt.get("retention")) or 0
                lim = num(lt.get("limit")) or 0
                rec = max(0.0, min(inc - ret, lim))
                if rec <= 0:
                    continue
                lines.append({**base(row), "claim": _s(first(row, CLAIM_NO)),
                              "layer": lay.get("name"), "layer_index": i,
                              "gross_premium": None, "sum_insured": None,
                              "ceded_pct": None, "ceded_premium": None,
                              "ceding_commission": None, "net_premium": None,
                              "claims_paid": None, "claims_outstanding": None,
                              "claim_incurred": _r(inc), "recovery": _r(rec)})

    elif ttype == "stop_loss":
        lay = layers[0] if layers else {"terms": {}}
        lt = lay.get("terms") or {}
        pool = ytd_rows if ytd_rows is not None else rows
        prem = sum(num(first(r, PREMIUM)) or 0 for r in pool)
        loss = sum(_incurred(r) or 0 for r in pool)
        if not any(_incurred(r) is not None for r in pool):
            notes.append("No claims data in the treaty year's bordereaux — no "
                         "recovery can be worked out yet.")
        if terms.get("measure") == "loss_ratio":
            base_prem = num(terms.get("subject_premium")) or prem
            lr = loss / base_prem * 100 if base_prem else None
            att, cov = num(lt.get("attachment_pct")) or 0, num(lt.get("limit_pct")) or 0
            pts = max(0.0, min((lr or 0) - att, cov))
            rec = pts / 100 * base_prem if base_prem else 0
            notes.append(f"Treaty year to date: premium {tt.full_money(base_prem)}, "
                         f"losses {tt.full_money(loss)}, loss ratio "
                         f"{(lr or 0):.2f}% against {att:g}%.")
        else:
            att, cov = num(lt.get("attachment_amount")) or 0, num(lt.get("limit_amount")) or 0
            rec = max(0.0, min(loss - att, cov))
            notes.append(f"Treaty year to date: aggregate losses "
                         f"{tt.full_money(loss)} against {tt.full_money(att)}.")
        if rec > 0:
            lines.append({"programme": None, "broker": None, "export_id": None,
                          "policy": None, "insured": None, "transaction": None,
                          "currency": None, "claim": None,
                          "layer": lay.get("name"), "layer_index": 0,
                          "gross_premium": _r(prem), "sum_insured": None,
                          "ceded_pct": None, "ceded_premium": None,
                          "ceding_commission": None, "net_premium": None,
                          "claims_paid": None, "claims_outstanding": None,
                          "claim_incurred": _r(loss), "recovery": _r(rec)})

    totals = _sum(lines)
    return {"lines": lines, "skipped": skipped, "notes": notes, "totals": totals}


def _incurred(row: dict) -> Optional[float]:
    v = num(first(row, CLAIM_INCURRED))
    if v is not None:
        return v
    paid, res = num(first(row, CLAIM_PAID)), num(first(row, CLAIM_RESERVE))
    if paid is None and res is None:
        return None
    return (paid or 0) + (res or 0)


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def _sum(lines: list[dict], factor: float = 1.0) -> dict:
    out = {}
    for k in MONEY_KEYS:
        vals = [ln.get(k) for ln in lines if ln.get(k) is not None]
        out[k] = round(sum(vals) * factor, 2) if vals else None
    out["lines"] = len(lines)
    return out


def by_reinsurer(structure: dict, lines: list[dict]) -> dict[int, dict]:
    """Each reinsurer's statement: every line × its share of that line's
    layer. {party_id: {"lines": [...], "totals": {...}, "shares": {...}}}"""
    out: dict[int, dict] = {}
    for i, lay in enumerate(structure.get("layers") or []):
        for sh in lay.get("shares") or []:
            pid = sh.get("reinsurer_party_id")
            pct = num(sh.get("share_pct")) or 0
            if not pid or pct <= 0:
                continue
            st = out.setdefault(pid, {"lines": [], "shares": {}})
            st["shares"][lay.get("name")] = pct
            for ln in lines:
                if (ln.get("layer_index") or 0) != i:
                    continue
                f = pct / 100
                mine = {**ln, "share_pct": pct}
                for k in MONEY_KEYS:
                    if ln.get(k) is not None:
                        mine[k] = round(ln[k] * f, 2)
                st["lines"].append(mine)
    for st in out.values():
        st["totals"] = _sum(st["lines"])
    return out


# ── the reinsurer's file ────────────────────────────────────────────────────
def _used_columns(lines: list[dict], columns: Optional[list[dict]]) -> list[tuple]:
    """[(key, heading)] — the setup's own choice and order, else every column
    that has something in it."""
    if columns:
        known = {k for k, _h, _kind in LINE_COLUMNS}
        return [(c["key"], c.get("heading") or c["key"]) for c in columns
                if c.get("key") in known]
    return [(k, h) for k, h, _kind in LINE_COLUMNS
            if any(ln.get(k) not in (None, "") for ln in lines)]


def statement_csv(lines: list[dict], columns: Optional[list[dict]]) -> bytes:
    """One reinsurer's lines as CSV, in the setup's columns."""
    import csv
    used = _used_columns(lines, columns)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for _k, h in used] + ["Your share %"])
    for ln in lines:
        w.writerow([ln.get(k) if ln.get(k) is not None else "" for k, _h in used]
                   + [ln.get("share_pct")])
    return buf.getvalue().encode("utf-8-sig")


def statement_xlsx(*, carrier: str, reinsurer: str, treaty: str,
                   type_label: str, period: str, version: int,
                   currency: Optional[str], shares: dict, totals: dict,
                   lines: list[dict], notes: list[str],
                   previous: Optional[dict] = None,
                   columns: Optional[list[dict]] = None) -> bytes:
    """One reinsurer's statement as a workbook: a summary, then its lines in
    the setup's columns (`columns` = [{key, heading}], in order)."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    bold = Font(bold=True)
    head = PatternFill("solid", fgColor="E1F1F3")
    rows = [
        ("Reinsurance bordereau", None),
        ("Reinsured", carrier), ("Reinsurer", reinsurer),
        ("Treaty", treaty), ("Type", type_label), ("Period", period),
        ("Version", version), ("Currency", currency or "as written"),
        ("Your share", ", ".join(f"{k}: {v:g}%" for k, v in shares.items())),
        (None, None),
    ]
    for k, v in rows:
        ws.append([k, v])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Totals (your share)", "This version"]
              + (["Previously sent", "Difference"] if previous else []))
    for c in ws[ws.max_row]:
        c.font = bold
        c.fill = head
    labels = {k: h for k, h, _kind in LINE_COLUMNS}
    for k in MONEY_KEYS:
        if totals.get(k) is None and not (previous and previous.get(k) is not None):
            continue
        cur = totals.get(k)
        row = [labels[k], cur]
        if previous:
            prev = previous.get(k)
            row += [prev, round((cur or 0) - (prev or 0), 2)]
        ws.append(row)
    ws.append(["Lines", totals.get("lines")])
    if notes:
        ws.append([])
        ws.append(["Notes"])
        ws[ws.max_row][0].font = bold
        for n in notes:
            ws.append([n])
    ws.column_dimensions["A"].width = 28
    for col in "BCD":
        ws.column_dimensions[col].width = 20

    ld = wb.create_sheet("Lines")
    used = _used_columns(lines, columns)
    ld.append([h for _k, h in used] + ["Your share %"])
    for c in ld[1]:
        c.font = bold
        c.fill = head
        c.alignment = Alignment(wrap_text=True)
    for ln in lines:
        ld.append([ln.get(k) for k, _h in used] + [ln.get("share_pct")])
    for i in range(1, len(used) + 2):
        ld.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 16
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
