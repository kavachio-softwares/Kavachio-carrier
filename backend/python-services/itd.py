"""ITD (inception-to-date) bordereaux — tell them apart, and load only what is new.

An ITD file repeats every transaction since the programme began, every month.
Run like a monthly file, it would count all of last month's rows again. This
module decides FROM THE ROWS (never the file name — the client's rule of
21 Aug 2026) whether a file is ITD, and compares it with what the feed has
already delivered — its running total, kept in `bdx_sent_transaction` — so only
new rows and money adjustments go on to validation, the output BDX and the
data model.

Pure functions, no database: itd_store.py reads and writes, test_itd.py tests
everything here directly.

THE SETUP'S itd_config (pipeline.itd_config, JSON — migration 38)
----------------------------------------------------------------
    {
      "mode": "never" | "auto" | "always",
      "on_changed": "adjust" | "flag",
      "adjust_suffix": "-ADJ",
      "reversal_suffixes": ["-R"],
      "sheets": {
        "ITD (H)": {
          "stream": "Schedule H premium",
          "key_columns": ["Risksmith Invoice Number"],
          "fallback_key_columns": ["Risksmith Policy Number",
                                   "Transaction Type - Original Premium etc",
                                   "Effective Date of Transaction US",
                                   "Reporting Start Date"],
          "period_column": "Reporting End Date",
          "transaction_date_column": "Effective Date of Transaction US",
          "money_columns": ["Palms Gross Written Premium $", "..."],
          "ignore_columns": []
        }
      }
    }

  mode          never (the default, and what a setup with no config has): the
                run is exactly what it was before ITD existed. auto: every file
                is looked at and ITD ones are handled as ITD. always: every
                file of this setup is ITD.
  on_changed    a row we already have whose money moved. adjust: send one
                adjustment row carrying only the difference. flag: send nothing
                and list it for a person.
  sheets        only the input sheets named here are compared; every other
                sheet the setup reads passes through untouched.
  stream        sheets with the same stream share one running total — for a
                workbook that carries the same transactions on a monthly tab
                AND an ITD tab. Defaults to the sheet's own name; auto_config
                gives such tabs one stream by itself (_share_streams).
  key_columns   the broker's own transaction id. Never an amount: if an amount
                were part of the key, a changed amount would look like a new
                row and be counted twice.
  fallback_key_columns
                used for a row with no transaction id (key_columns empty, or a
                sheet with no id column at all) — the client's business key
                (Open Questions Register): policy, transaction type, effective
                date, reported period, amount. The rows sharing the first four
                are matched with what was delivered under them in three steps
                (_match_composite): all five the same (the amounts too) →
                the same transaction, whatever order the rows come in; then
                what is left pairs on the four alone → the amount CHANGED,
                only the difference goes on; what is still left is new (or,
                in an ITD, missing). The amount is never the key itself: a
                corrected amount would then read as a new transaction plus a
                missing one and be counted twice. Rows alike on all five are
                all kept (#1, #2, …) — identical-looking rows can be real (the
                Risksmith ITD has three pairs alike on all five) — and a new
                one is noted for a person to confirm.
  period_column, transaction_date_column
                the bordereau's reported period and the transaction date, read
                TOGETHER to tell an ITD file from a monthly one (the client's
                rule, 21 Aug 2026 — see detect()): transaction dates over many
                months make a file a candidate, the reported period decides —
                many reported periods is ITD, one is a monthly file whose
                earlier-dated rows are late transactions, loaded as usual.
                Transaction dates alone never make a file ITD; a file whose
                reported period cannot be read is regular.
  reversal_suffixes
                a transaction id ending in one of these (INV110582-R) reverses
                the transaction without it (INV110582): it is classed REVERSAL
                and linked to that transaction, so the original and the
                reversal stay together for audit. [] switches this off.
  money_columns each is compared and adjusted on its own.
  ignore_columns never count as a change (a print date, a run number).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Optional

MODES = ("never", "auto", "always")
ON_CHANGED = ("adjust", "flag")

ITD = "itd"
REGULAR = "regular"

# What happened to each row of a file, compared with the running total.
NEW = "new"
REVERSAL = "reversal"                # a new row reversing an earlier transaction
UNCHANGED = "unchanged"
CHANGED = "changed"                  # money moved → an adjustment row
DETAILS_CHANGED = "details_changed"  # same money, other fields moved → listed only
DROPPED = "dropped"                  # in the running total, missing from this ITD
DUPLICATE = "duplicate"              # the same key twice in one file
MISSING_KEY = "missing_key"          # no key column and no fallback filled in
RESULTS = (NEW, REVERSAL, UNCHANGED, CHANGED, DETAILS_CHANGED, DROPPED, DUPLICATE,
           MISSING_KEY)
# The client's classes (Open Questions Register: "Classify each row as New /
# Previously processed / Changed / Cancelled or reversed") — what the screens
# and summaries show. The results above stay underneath, because they drive
# the work: only a CHANGED amount makes an adjustment row, only a DROPPED
# transaction waits for a person. A duplicate in the file and a row with no
# transaction id fit none of the four, so they are SKIPPED (class key held_back).
CLASS_NEW = "new"
CLASS_PREVIOUS = "previously_processed"
CLASS_CHANGED = "changed"
CLASS_CANCELLED = "cancelled_or_reversed"
CLASS_HELD = "held_back"
CLASSES = (CLASS_NEW, CLASS_PREVIOUS, CLASS_CHANGED, CLASS_CANCELLED, CLASS_HELD)
CLASS_OF = {
    NEW: CLASS_NEW,
    UNCHANGED: CLASS_PREVIOUS,
    CHANGED: CLASS_CHANGED, DETAILS_CHANGED: CLASS_CHANGED,
    REVERSAL: CLASS_CANCELLED, DROPPED: CLASS_CANCELLED,
    DUPLICATE: CLASS_HELD, MISSING_KEY: CLASS_HELD,
}


def results_in(cls: str) -> list[str]:
    """The results one of the client's classes is made of."""
    return [r for r, c in CLASS_OF.items() if c == cls]


# Results whose rows go on as they came (an ITD file's other kept rows are
# adjustments).
SENT_AS_IS = (NEW, REVERSAL)
DEFAULT_REVERSAL_SUFFIXES = ("-R",)

# Columns carried on the rows of an ITD delta so the data-model load knows each
# row's key. Never part of the file's own column list, so nothing shows them.
ROW_KEY_COL = "__itd_row_key"
SOURCE_REF_COL = "__itd_source_ref"
ADJUSTS_COL = "__itd_adjusts_key"
REVERSES_COL = "__itd_reverses_key"
RESERVED_COLUMNS = (ROW_KEY_COL, SOURCE_REF_COL, ADJUSTS_COL, REVERSES_COL)
_SOURCE_SHEET_KEY = "__source_sheet__"     # direct_lane.SOURCE_SHEET_KEY

_CENT = Decimal("0.01")


# ── config ──────────────────────────────────────────────────────────────────

def _names(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x).strip() for x in v if x is not None and str(x).strip()]


def normalize_config(raw: Any) -> Optional[dict]:
    """A stored or submitted itd_config, cleaned. None for no config at all.

    Anything unreadable is treated as no config — i.e. ITD off — so a malformed
    value can never start filtering a setup's rows without anybody asking. A
    sheet with neither key_columns nor fallback_key_columns is dropped: there
    would be no way to recognise any of its rows."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    mode = str(raw.get("mode") or "never").strip().lower()
    on_changed = str(raw.get("on_changed") or "adjust").strip().lower()
    suffix = raw.get("adjust_suffix")
    rev = raw.get("reversal_suffixes")
    sheets: dict[str, dict] = {}
    for name, sc in (raw.get("sheets") or {}).items():
        if not isinstance(sc, dict) or not str(name).strip():
            continue
        cfg = {
            "stream": (str(sc.get("stream")).strip() if sc.get("stream") else "")
                      or str(name).strip(),
            "key_columns": _names(sc.get("key_columns")),
            "fallback_key_columns": _names(sc.get("fallback_key_columns")),
            "period_column": (str(sc["period_column"]).strip()
                              if sc.get("period_column") else None),
            "transaction_date_column": (str(sc["transaction_date_column"]).strip()
                                        if sc.get("transaction_date_column") else None),
            "money_columns": _names(sc.get("money_columns")),
            "ignore_columns": _names(sc.get("ignore_columns")),
        }
        if cfg["key_columns"] or cfg["fallback_key_columns"]:
            sheets[str(name).strip()] = cfg
    return {
        "mode": mode if mode in MODES else "never",
        "on_changed": on_changed if on_changed in ON_CHANGED else "adjust",
        "adjust_suffix": (str(suffix) if isinstance(suffix, str) and suffix.strip()
                          else "-ADJ"),
        "reversal_suffixes": (_names(rev) if rev is not None
                              else list(DEFAULT_REVERSAL_SUFFIXES)),
        "sheets": sheets,
        # True while the settings are the ones auto_config() picked when the
        # setup was created and nobody has saved them since.
        "auto_filled": raw.get("auto_filled") is True,
    }


def enabled(cfg: Optional[dict]) -> bool:
    """Is ITD handling switched on for this setup at all?"""
    return bool(cfg and cfg.get("mode") in ("auto", "always") and cfg.get("sheets"))


def _norm_name(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _sheet_cfg(cfg: dict, sheet_name: str) -> Optional[dict]:
    want = _norm_name(sheet_name)
    for name, sc in (cfg.get("sheets") or {}).items():
        if _norm_name(name) == want:
            return sc
    return None


def _column(row: dict, name: Optional[str]) -> Optional[str]:
    """The row's own spelling of a configured column (case and spacing
    forgiven — 'Insured  Company Name' and 'insured company name' are one)."""
    if not name:
        return None
    if name in row:
        return name
    want = _norm_name(name)
    for k in row:
        if _norm_name(k) == want:
            return k
    return None


def _value(row: dict, name: Optional[str]) -> Any:
    col = _column(row, name)
    return row.get(col) if col is not None else None


# ── values ──────────────────────────────────────────────────────────────────

_NUMERIC = re.compile(r"^[-+]?\d+(\.\d+)?$")
_ISO_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]00:00(:00(\.0+)?)?$")


def _blank(v: Any) -> bool:
    # An empty Excel cell can arrive as NaN when a sheet is read without the
    # landing's clean-up — it is blank, never a number.
    return (v is None or (isinstance(v, str) and not v.strip())
            or (isinstance(v, float) and v != v))


def _num_text(f: float) -> str:
    f = round(f, 6)               # 24999999.999999996 and 25000000 are one value
    if f == int(f):
        return str(int(f))
    return f"{f:.6f}".rstrip("0").rstrip(".")


def norm_value(v: Any) -> Optional[str]:
    """One spelling per value, for keys and change detection: blank → None,
    98000 / 98000.0 / '98000' → '98000', '2025-08-01T00:00:00' → '2025-08-01',
    case and inner spacing forgiven."""
    if _blank(v):
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, Decimal)):
        return _num_text(float(v))
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = " ".join(str(v).split())
    m = _ISO_MIDNIGHT.match(s)
    if m:
        return m.group(1)
    if _NUMERIC.match(s):
        return _num_text(float(s))
    return s.casefold()


def to_number(v: Any) -> Optional[float]:
    """'10,000', '$1,500.50', '(250)', 98000 → a float; None when not a number."""
    if _blank(v) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "").replace("£", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def _cents(v: Any) -> Decimal:
    f = to_number(v)
    if f is None:
        return Decimal("0.00")
    return Decimal(str(f)).quantize(_CENT, rounding=ROUND_HALF_UP)


_EXCEL_EPOCH = date(1899, 12, 30)


def period_of(v: Any) -> Optional[str]:
    """'YYYY-MM' of a date-like cell: a date, an ISO string, '20250801',
    '08/31/2025' (US first, day-first when the first part is over 12), an
    Excel serial number, or a period label that is already 'YYYY-MM'."""
    if _blank(v):
        return None
    if isinstance(v, (datetime, date)):
        return f"{v.year:04d}-{v.month:02d}"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 20000 <= float(v) <= 80000:          # an Excel serial date
            d = _EXCEL_EPOCH + timedelta(days=int(float(v)))
            return f"{d.year:04d}-{d.month:02d}"
        v = str(int(v)) if float(v) == int(v) else str(v)
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})(?:\D|$)", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        month = b if a > 12 else a
        if 1 <= month <= 12:
            return f"{y:04d}-{month:02d}"
    m = re.match(r"^\d{1,2}[ -]([A-Za-z]{3})[A-Za-z]*[ -](\d{4})$", s)
    if m:
        try:
            return datetime.strptime(f"1 {m.group(1)} {m.group(2)}", "%d %b %Y").strftime("%Y-%m")
        except ValueError:
            return None
    return None


def period_bounds(periods: Iterable[str]) -> tuple[Optional[date], Optional[date]]:
    """First day of the earliest period, last day of the latest one."""
    ps = sorted(p for p in periods if p)
    if not ps:
        return None, None
    y0, m0 = map(int, ps[0].split("-"))
    y1, m1 = map(int, ps[-1].split("-"))
    nxt = date(y1 + (m1 == 12), 1 if m1 == 12 else m1 + 1, 1)
    return date(y0, m0, 1), nxt - timedelta(days=1)


def label_period(label: Optional[str]) -> Optional[str]:
    """A calendar period label as 'YYYY-MM' ('2026-09' stays; '2026-Q1' → its
    last month, '2026-Q1' being due once March has ended)."""
    if not label:
        return None
    s = str(label).strip()
    m = re.match(r"^(\d{4})-Q([1-4])$", s, re.I)
    if m:
        return f"{m.group(1)}-{int(m.group(2)) * 3:02d}"
    return period_of(s)


# ── detection ───────────────────────────────────────────────────────────────

def _dates(rows: list[dict], sc: dict) -> list[tuple[Optional[str], Optional[str]]]:
    """(reported period, transaction month) of each row — the two columns the
    client's rule (21 Aug 2026) reads together. Either is None when its column
    is not configured or the row's cell cannot be read as a date."""
    pc, tc = sc.get("period_column"), sc.get("transaction_date_column")
    return [(period_of(_value(r, pc)) if pc else None,
             period_of(_value(r, tc)) if tc else None) for r in rows]


def _row_period(row: dict, sc: Optional[dict]) -> Optional[str]:
    """'YYYY-MM' the row is reported in, or None when it cannot be read."""
    pc = (sc or {}).get("period_column")
    return period_of(_value(row, pc)) if pc else None


def _span(ps: list[str]) -> str:
    return f"{ps[0]} to {ps[-1]}" if len(ps) > 1 else (ps[0] if ps else "")


def _months_text(ps: list[str]) -> str:
    return f"{len(ps)} month{'' if len(ps) == 1 else 's'} ({_span(ps)})"


def detect(landing: dict, cfg: Optional[dict],
           file_period: Optional[str] = None) -> dict:
    """Is this file ITD? Decided from its rows, reading two columns together
    (the client's rule, 21 Aug 2026): the transaction date and the bordereau's
    reported period.

      transaction dates in one month                → regular
      transaction dates over many months, and the
        reported period over many months too        → ITD: the rows were
                                                       reported in earlier
                                                       bordereaux — history
      transaction dates over many months, but every
        row reported in ONE period                  → regular: the earlier-
                                                       dated rows are late
                                                       transactions reported
                                                       this month — loaded,
                                                       never taken for history
      the reported period over many months, whatever
        the transaction dates                       → ITD
      no reported period on the rows                → regular, every row
                                                       loaded

    So transaction dates spread over many months make a file a candidate, and
    the reported period decides: a late transaction carries this month's
    reported period (the Risksmith monthly tab: 28 rows, transaction dates
    over 10 months, all reported 2025-08). Transaction dates are never enough
    on their own — judged on them alone, that monthly tab would be ITD and its
    late rows taken for history. Only the sheets the config names are read.

    Returns {"is_itd", "reason", "periods" (distinct reported periods,
    sorted), "as_at" (the latest of them — what an ITD is "as at"), "basis",
    "transaction_periods" (distinct transaction months), "late_rows" (rows
    whose transaction month is before the period they are reported in)}."""
    out = {"is_itd": False, "reason": None, "periods": [], "as_at": None,
           "basis": None, "transaction_periods": [], "late_rows": 0}
    if not enabled(cfg):
        out["reason"] = "ITD handling is off for this setup."
        return out
    pairs: list[tuple[Optional[str], Optional[str]]] = []
    col = ""                       # the reported-period column, as the setup names it
    for name, sheet in ((landing or {}).get("sheets") or {}).items():
        sc = _sheet_cfg(cfg, name)
        if sc is None:
            continue
        got = _dates(sheet.get("rows") or [], sc)
        pairs.extend(got)
        if not col and any(p for p, _ in got):
            col = sc["period_column"]
    reported = sorted({p for p, _ in pairs if p})
    txn = sorted({t for _, t in pairs if t})
    late = sum(1 for p, t in pairs if p and t and t < p)
    out.update(periods=reported, transaction_periods=txn, late_rows=late,
               as_at=reported[-1] if reported else label_period(file_period),
               basis=("reported period" + (" and transaction date" if txn else ""))
                     if reported else None)
    seen_txn = (f"transaction dates over {_months_text(txn)}" if len(txn) > 1
                else f"transaction dates in {txn[0]}" if txn else "")
    if cfg["mode"] == "always":
        out["is_itd"] = True
        out["reason"] = ("The setup says this broker always sends ITD files"
                         + (f" (rows reported {_span(reported)})." if reported else "."))
        return out
    if len(reported) > 1:
        out["is_itd"] = True
        out["reason"] = (f"Rows are reported in {len(reported)} months, "
                         f"{_span(reported)} ({col})"
                         + (f", with {seen_txn}" if seen_txn else "")
                         + " — the file repeats earlier bordereaux, so it is ITD.")
    elif reported:
        if len(txn) > 1:
            out["reason"] = (
                f"The rows have {seen_txn}, but every row is reported in "
                f"{reported[0]} ({col}) — a monthly file."
                + (f" {late} row{' is a late transaction' if late == 1 else 's are late transactions'}"
                   f" reported this month — loaded as usual, not taken for history."
                   if late else ""))
        else:
            out["reason"] = (f"Every row is reported in {reported[0]} ({col})"
                             + (f", with {seen_txn}" if seen_txn else "")
                             + " — a monthly file.")
    elif len(txn) > 1:
        out["reason"] = (f"The rows have {seen_txn}, but no reported period could be "
                         f"read, so the file is treated as a monthly one and every row "
                         f"is loaded. Transaction dates alone cannot tell a late "
                         f"transaction from history — if this is an ITD file, the "
                         f"broker needs to fill in the reported period.")
    else:
        out["reason"] = ("No reported period could be read from the rows, so the "
                         "file is treated as a monthly one and every row is loaded.")
    return out


# ── one row ─────────────────────────────────────────────────────────────────

def make_key(row: dict, sc: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(row_key, key_source, source_ref) for one row.

    key_source 'txn' when the broker's own id is there, 'composite' when only
    the fallback columns are, and (None, None, None) when neither is filled in.
    source_ref is the id as the broker wrote it (for screens and the data
    model), the row_key its normalised form (for matching)."""
    cols = sc.get("key_columns") or []
    if cols:
        vals = [norm_value(_value(row, c)) for c in cols]
        if all(vals):
            ref = " / ".join(str(_value(row, c)).strip() for c in cols)
            return "txn:" + "|".join(vals), "txn", ref
    fcols = sc.get("fallback_key_columns") or []
    if fcols:
        vals = [norm_value(_value(row, c)) for c in fcols]
        if all(vals):
            ref = " / ".join(str(_value(row, c)).strip() for c in fcols)
            return "cmp:" + "|".join(vals), "composite", ref
    return None, None, None


def _blank_columns(row: dict, cols: Optional[list]) -> list[str]:
    """The id columns that are blank on a row, as the setup names them."""
    return [c for c in cols or [] if norm_value(_value(row, c)) is None]


def reversal_of(row: dict, sc: dict, suffixes: Iterable[str]
                ) -> Optional[tuple[str, str]]:
    """(row key, id as written) of the transaction this row reverses, when its
    transaction id ends in a reversal suffix: INV110582-R → ('txn:inv110582',
    'INV110582'). None for any other row. Only for a single transaction-id
    column — a reversal is recognised by the broker's own id."""
    cols = sc.get("key_columns") or []
    if len(cols) != 1:
        return None
    raw = _value(row, cols[0])
    if _blank(raw):
        return None
    ident = str(raw).strip()
    for suf in suffixes or ():
        suf = str(suf).strip()
        if suf and len(ident) > len(suf) and ident.upper().endswith(suf.upper()):
            base = ident[:-len(suf)].rstrip()
            nb = norm_value(base)
            if nb:
                return "txn:" + nb, base
    return None


def _comparable(row: dict, sc: dict) -> dict[str, str]:
    skip = {_norm_name(c) for c in (sc.get("ignore_columns") or [])}
    skip.update(_norm_name(c) for c in RESERVED_COLUMNS)
    skip.add(_norm_name(_SOURCE_SHEET_KEY))
    out: dict[str, str] = {}
    for k, v in row.items():
        nk = _norm_name(k)
        if nk in skip:
            continue
        nv = norm_value(v)
        if nv is not None:          # a blank cell and a missing column are one
            out[nk] = nv
    return out


def row_hash(row: dict, sc: dict) -> str:
    """Fingerprint of everything on the row except ignored columns — the same
    hash means the broker re-sent the row unchanged."""
    payload = json.dumps(sorted(_comparable(row, sc).items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def money_of(row: dict, sc: dict) -> dict[str, float]:
    """The configured money columns, to the cent. A blank cell counts as 0."""
    return {c: float(_cents(_value(row, c))) for c in (sc.get("money_columns") or [])}


def money_delta(old: Optional[dict], new: Optional[dict]) -> dict[str, float]:
    """new − old for each money column, to the cent. Zero differences are left
    out, so an empty result means no money moved."""
    old, new = old or {}, new or {}
    out: dict[str, float] = {}
    for c in list(dict.fromkeys([*old.keys(), *new.keys()])):
        d = _cents(new.get(c)) - _cents(old.get(c))
        if d != 0:
            out[c] = float(d)
    return out


def changed_fields(old_values: Optional[dict], new_values: Optional[dict],
                   sc: dict) -> list[str]:
    """The columns whose value differs, in the new row's order."""
    a = _comparable(old_values or {}, sc)
    b = _comparable(new_values or {}, sc)
    names = {_norm_name(k): k for k in (new_values or {})}
    names.update({_norm_name(k): k for k in (old_values or {}) if _norm_name(k) not in names})
    return [names.get(k, k) for k in dict.fromkeys([*b.keys(), *a.keys()])
            if a.get(k) != b.get(k)]


def _plain(row: dict) -> dict:
    return {k: v for k, v in row.items()
            if k not in RESERVED_COLUMNS and k != _SOURCE_SHEET_KEY}


def _known_money(pos: dict, sc: dict) -> dict[str, float]:
    """What was delivered for each money column of this transaction. A column
    recognised as money only after the transaction was delivered has no entry
    in the position's money — its delivered amount is read from the row as it
    was delivered (the position's values), so it is not taken for a jump from
    0 to its whole amount."""
    old = dict(pos.get("money") or {})
    values = pos.get("values") or {}
    for c in sc.get("money_columns") or []:
        if c not in old and values:
            old[c] = float(_cents(_value(values, c)))
    return old


def refresh_money_columns(cfg: Optional[dict], landing: dict) -> Optional[dict]:
    """Settings filled in automatically (auto_filled) take up amount columns
    recognised since they were filled in — read from the headings of the file
    being run, as auto_config reads them. A person's own settings are left as
    they are, and so are headings a sheet ignores (a heading only one of two
    linked tabs has)."""
    if not cfg or not cfg.get("auto_filled"):
        return cfg
    out = copy.deepcopy(cfg)
    for name, sheet in ((landing or {}).get("sheets") or {}).items():
        sc = _sheet_cfg(out, name)
        if sc is None:
            continue
        have = {_norm_name(c) for c in sc["money_columns"]}
        skip = {_norm_name(c) for c in sc["ignore_columns"]} | {_norm_name(c) for c in RESERVED_COLUMNS}
        for col in sheet.get("columns") or []:
            n = _norm_name(col)
            if n not in have and n not in skip and _is_money_heading(n):
                sc["money_columns"].append(str(col))
                have.add(n)
    return out


# ── the comparison ──────────────────────────────────────────────────────────

def _against(r: dict, row: dict, sc: dict, pos: dict) -> dict:
    """A row matched with what was delivered before: UNCHANGED, CHANGED (money
    moved — an adjustment row) or DETAILS_CHANGED. `r` carries new_hash and
    new_money; it is filled in and returned."""
    old_money = _known_money(pos, sc)
    r.update(position_id=pos.get("position_id"), old_hash=pos.get("hash"),
             old_money=old_money)
    if pos.get("hash") == r["new_hash"]:
        r["result"] = UNCHANGED
        return r
    delta = money_delta(old_money, r["new_money"])
    r.update(result=CHANGED if delta else DETAILS_CHANGED, delta=delta or None,
             changed_fields=changed_fields(pos.get("values"), row, sc),
             old_values=pos.get("values"), new_values=_plain(row),
             adjust_no=(int(pos.get("adjust_count") or 0) + 1) if delta else None)
    return r


def _split_composite(key: str) -> tuple[str, Optional[int]]:
    """'cmp:pol|type|date|period#2' → ('cmp:pol|type|date|period', 2)."""
    head, sep, n = key.rpartition("#")
    if sep and n.isdigit():
        return head, int(n)
    return key, None


def _composite_index(positions: dict) -> dict[tuple[str, str], list[tuple[int, str, dict]]]:
    """Delivered transactions with no transaction id, by business key:
    [(number, row key, position)], numbered as they were first delivered."""
    out: dict[tuple[str, str], list[tuple[int, str, dict]]] = {}
    for (stream, key), pos in positions.items():
        if not key or not key.startswith("cmp:"):
            continue
        head, n = _split_composite(key)
        if n is not None:
            out.setdefault((stream, head), []).append((n, key, pos))
    for v in out.values():
        v.sort(key=lambda t: t[0])
    return out


def _amounts(money: Optional[dict], sc: dict) -> tuple:
    """The amounts of a row — the business key's fifth field — comparable
    whichever order the columns were stored in."""
    m = money or {}
    return tuple((c, float(m.get(c, 0.0))) for c in (sc.get("money_columns") or []))


def _match_composite(stream: str, gkey: str, items: list[dict],
                     index: dict, seen: set) -> None:
    """Settle the rows of one business key (policy, transaction type, date,
    reported period — no transaction id) against what was delivered under it.
    Fills in each item's result in place.

      0. On a later sheet of the same stream, a row alike on all five fields
         (the amounts too) to one on an earlier sheet is that row again — the
         monthly tab and the ITD tab of one workbook — counted once
         (DUPLICATE, as for a transaction id).
      1. A row alike on all five to a delivered one IS that one: unchanged,
         or only its details moved. Whole rows are paired first, then rows
         alike on the amounts. The order the rows come in plays no part.
      2. What is left pairs on the four alone, in file order: the same
         transaction with its amount changed — only the difference goes on.
      3. A row still left is NEW (a fresh number, never one in use); a
         delivered one still left is missing from the file (DROPPED, for an
         ITD — the caller lists those).

    Rows alike on all five are all kept, never merged: they can be real (an
    original and its re-issue). A NEW one is noted (identical_rows) for a
    person to confirm it is not the same row sent twice."""
    # 0. one workbook, two tabs, the same transaction
    kept: list[dict] = []
    earlier: dict[tuple, list[str]] = {}       # amounts → sheets holding a copy
    sheet, pool, this_sheet = None, {}, []
    for it in items:
        r = it["r"]
        it["amounts"] = _amounts(r["new_money"], it["sc"])
        if r["sheet"] != sheet:
            for a, name in this_sheet:
                earlier.setdefault(a, []).append(name)
            sheet, this_sheet = r["sheet"], []
            pool = {a: list(names) for a, names in earlier.items()}
        if pool.get(it["amounts"]):
            r.update(result=DUPLICATE, duplicate_of_sheet=pool[it["amounts"]].pop(0))
            continue
        this_sheet.append((it["amounts"], r["sheet"]))
        kept.append(it)

    delivered = index.get((stream, gkey), [])
    taken = {n for n, _, _ in delivered}       # removed ones too: never reused
    free = [(n, key, pos) for n, key, pos in delivered
            if pos.get("status") not in ("void", "removed")]
    pairs: list[tuple[dict, tuple[int, str, dict]]] = []
    used: set[str] = set()

    def pair(same) -> None:
        for it in kept:
            if "match" in it:
                continue
            for cand in free:
                if cand[1] not in used and same(it, cand[2]):
                    it["match"] = cand
                    used.add(cand[1])
                    pairs.append((it, cand))
                    break

    # 1. alike on all five: the whole row, then the amounts
    pair(lambda it, pos: pos.get("hash") == it["r"]["new_hash"])
    pair(lambda it, pos: _amounts(_known_money(pos, it["sc"]), it["sc"]) == it["amounts"])
    # 2. alike on the four: the amount changed
    pair(lambda it, pos: True)

    for it, (n, key, pos) in pairs:
        it["r"]["row_key"] = key
        seen.add((stream, key))
        _against(it["r"], it["row"], it["sc"], pos)

    # 3. new — numbered after every number already in use
    nxt = max(taken, default=0)
    alike = Counter(it["amounts"] for it in kept)
    for it in kept:
        if "match" in it:
            continue
        nxt += 1
        key = f"{gkey}#{nxt}"
        it["r"].update(row_key=key, result=NEW, new_values=_plain(it["row"]))
        seen.add((stream, key))
        if alike[it["amounts"]] > 1:
            it["r"]["identical_rows"] = alike[it["amounts"]]


def compare(landing: dict, cfg: dict, positions: dict, *, load_type: str) -> dict:
    """Compare a file with its feed's running total.

    `positions` is {(stream, row_key): {"position_id", "hash", "money",
    "values", "status", "adjust_count"}} — what itd_store.load_positions
    returns. Positions marked void or removed count as never seen.

    A row seen for the first time whose id ends in a reversal suffix is
    REVERSAL, not NEW: it goes on like a new row, and carries reverses_key —
    the transaction it reverses, when that is in this file or already
    delivered (None when it is in neither) — and reverses_ref, that
    transaction's id as written.

    Returns {"results": [...], "summary": {...}}. One result per row of every
    configured sheet, plus one per position this ITD no longer carries
    (DROPPED — never for a regular file, which only ever holds its own month).
    Every result carries what itd_store needs to write the event and, later,
    move the running total forward — and "row_period", the month the row is
    reported in (its reported-period column), so a screen can tell this
    month's rows from the ones the ITD file repeats."""
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    # Rows with no transaction id, by business key — settled once the whole
    # file is read, because which old row each one is depends on all of them.
    groups: dict[tuple[str, str], list[dict]] = {}
    streams: set[str] = set()
    suffixes = (cfg or {}).get("reversal_suffixes") or ()
    first_sheet: dict[tuple[str, str], str] = {}
    stream_sc: dict[str, dict] = {}
    for sheet_name, sheet in ((landing or {}).get("sheets") or {}).items():
        sc = _sheet_cfg(cfg, sheet_name)
        if sc is None:
            continue
        stream = sc["stream"]
        streams.add(stream)
        stream_sc.setdefault(stream, sc)
        for idx, row in enumerate(sheet.get("rows") or []):
            base = {"stream": stream, "sheet": sheet_name, "row_no": idx,
                    "row_key": None, "key_source": None, "source_ref": None,
                    "old_hash": None, "new_hash": None, "old_money": None,
                    "new_money": None, "delta": None, "changed_fields": None,
                    "adjust_no": None, "position_id": None,
                    "old_values": None, "new_values": None,
                    "reverses_key": None, "reverses_ref": None,
                    "duplicate_of_sheet": None, "row_period": _row_period(row, sc)}
            key, source, ref = make_key(row, sc)
            if key is None:
                # Which id columns were blank, so the note can name them.
                results.append({**base, "result": MISSING_KEY,
                                "blank_key_columns": _blank_columns(row, sc.get("key_columns")),
                                "blank_fallback_columns": _blank_columns(
                                    row, sc.get("fallback_key_columns"))})
                continue
            base.update(row_key=key, key_source=source, source_ref=ref)
            if source == "composite":
                r = {**base, "result": None, "new_hash": row_hash(row, sc),
                     "new_money": money_of(row, sc)}
                results.append(r)
                groups.setdefault((stream, key), []).append({"r": r, "row": row, "sc": sc})
                continue
            sk = (stream, key)
            if sk in seen:
                # On another sheet of the same stream (a monthly tab and an ITD
                # tab carrying the same transaction): counted once, from there.
                other = first_sheet.get(sk)
                results.append({**base, "result": DUPLICATE,
                                "duplicate_of_sheet": other if other != sheet_name else None})
                continue
            seen.add(sk)
            first_sheet[sk] = sheet_name
            h = row_hash(row, sc)
            money = money_of(row, sc)
            base.update(new_hash=h, new_money=money)
            pos = positions.get(sk)
            if pos is not None and pos.get("status") in ("void", "removed"):
                pos = None
            if pos is None:
                rev = reversal_of(row, sc, suffixes) if source == "txn" else None
                if rev is not None:
                    results.append({**base, "result": REVERSAL, "new_values": _plain(row),
                                    "reverses_key": rev[0], "reverses_ref": rev[1]})
                else:
                    results.append({**base, "result": NEW, "new_values": _plain(row)})
                continue
            results.append(_against(dict(base), row, sc, pos))
    if groups:
        index = _composite_index(positions)
        for (stream, gkey), items in groups.items():
            _match_composite(stream, gkey, items, index, seen)
    # A reversal is linked only to a transaction we can point at: one in this
    # file, or one already delivered.
    for r in results:
        if r["result"] == REVERSAL:
            k = (r["stream"], r["reverses_key"])
            known = positions.get(k)
            if k not in seen and (known is None or known.get("status") == "void"):
                r["reverses_key"] = None
    if load_type == ITD:
        for (stream, key), pos in positions.items():
            if (stream in streams and (stream, key) not in seen
                    and pos.get("status") == "active"):
                results.append({
                    "stream": stream, "sheet": None, "row_no": None,
                    "row_key": key, "key_source": pos.get("key_source"),
                    "source_ref": pos.get("source_ref"), "result": DROPPED,
                    "old_hash": pos.get("hash"), "new_hash": None,
                    "old_money": pos.get("money") or {}, "new_money": None,
                    "delta": None, "changed_fields": None, "adjust_no": None,
                    "position_id": pos.get("position_id"),
                    "old_values": None, "new_values": None,
                    "reverses_key": None, "reverses_ref": None,
                    "duplicate_of_sheet": None,
                    # Not in this file: the month it was reported in before.
                    "row_period": _row_period(pos.get("values") or {},
                                              stream_sc.get(stream))})
    return {"results": results, "summary": summarize(results, cfg, load_type)}


def summarize(results: list[dict], cfg: Optional[dict], load_type: str) -> dict:
    """Counts for the screens: how many rows of each kind, how many go on."""
    counts = Counter(r["result"] for r in results)
    adjust = (cfg or {}).get("on_changed", "adjust") == "adjust"
    in_file = sum(n for k, n in counts.items() if k != DROPPED)
    if load_type == ITD:
        to_load = counts[NEW] + counts[REVERSAL] + (counts[CHANGED] if adjust else 0)
    else:
        to_load = in_file
    money: dict[str, float] = {}
    for r in results:
        if r["result"] == CHANGED and adjust:
            for c, d in (r.get("delta") or {}).items():
                money[c] = float(Decimal(str(money.get(c, 0))) + Decimal(str(d)))
    return {
        "load_type": load_type,
        "rows_in_file": in_file,
        "rows_to_load": to_load,
        **{k: counts.get(k, 0) for k in RESULTS},
        # The same rows in the client's classes.
        "classes": {c: sum(counts.get(r, 0) for r in results_in(c)) for c in CLASSES},
        # Of the duplicates: the ones whose first copy is on another sheet.
        "duplicate_other_sheet": sum(1 for r in results if r["result"] == DUPLICATE
                                     and r.get("duplicate_of_sheet")),
        # New rows with no transaction id, alike on every field of the
        # business key to another row of the file — kept, for a person to
        # confirm they are real and not one row sent twice.
        "identical_rows": sum(1 for r in results if r.get("identical_rows")),
        "adjustment_totals": money,
    }


def build_delta(landing: dict, cfg: dict, comparison: dict, *, load_type: str,
                as_at: Optional[str] = None) -> dict:
    """The landing the run actually processes.

    REGULAR: the file exactly as it came — the normal flow is untouched; the
    comparison is only kept so the running total knows these rows.
    ITD: for every configured sheet, only NEW and REVERSAL rows (as they
    came; a reversal carries REVERSES_COL, the transaction it reverses) and, for
    CHANGED rows when on_changed is 'adjust', one adjustment row each: the row
    with every money column set to its difference (0 where that column did
    not move) and, when the key is the broker's own id, that id suffixed
    (INV110582 → INV110582-ADJ1) so the output shows what the row is. Sheets
    the config does not name pass through untouched.

    Each delta row carries ROW_KEY_COL / SOURCE_REF_COL / ADJUSTS_COL /
    REVERSES_COL for the
    data-model load. The landing gains an "itd" block describing the run."""
    out = copy.deepcopy(landing or {})
    summary = comparison.get("summary") or {}
    if load_type == ITD:
        by_sheet: dict[str, list[dict]] = {}
        for r in comparison.get("results") or []:
            if r.get("sheet") is not None:
                by_sheet.setdefault(r["sheet"], []).append(r)
        suffix = cfg.get("adjust_suffix") or "-ADJ"
        adjust = cfg.get("on_changed", "adjust") == "adjust"
        for sheet_name, sheet in (out.get("sheets") or {}).items():
            sc = _sheet_cfg(cfg, sheet_name)
            if sc is None:
                continue
            rows = sheet.get("rows") or []
            kept: list[dict] = []
            for r in sorted(by_sheet.get(sheet_name, []), key=lambda x: x["row_no"]):
                row = rows[r["row_no"]]
                if r["result"] in SENT_AS_IS:
                    kept.append({**row, ROW_KEY_COL: r["row_key"],
                                 SOURCE_REF_COL: r["source_ref"], ADJUSTS_COL: None,
                                 REVERSES_COL: r.get("reverses_key")})
                elif r["result"] == CHANGED and adjust:
                    n = r["adjust_no"] or 1
                    adj = dict(row)
                    for c in sc.get("money_columns") or []:
                        col = _column(adj, c) or c
                        adj[col] = (r.get("delta") or {}).get(c, 0.0)
                    ref = r["source_ref"]
                    if r["key_source"] == "txn" and len(sc.get("key_columns") or []) == 1:
                        kc = _column(adj, sc["key_columns"][0])
                        if kc is not None:
                            adj[kc] = f"{str(row.get(kc)).strip()}{suffix}{n}"
                            ref = adj[kc]
                    else:
                        ref = f"{ref}{suffix}{n}"
                    adj.update({ROW_KEY_COL: f"{r['row_key']}#ADJ{n}",
                                SOURCE_REF_COL: ref, ADJUSTS_COL: r["row_key"],
                                REVERSES_COL: None})
                    kept.append(adj)
            sheet["rows"] = kept
        out["row_count"] = sum(len(sh.get("rows") or []) for sh in out["sheets"].values())
    out["itd"] = {"load_type": load_type, "as_at": as_at, "summary": summary,
                  "original_row_count": (landing or {}).get("row_count")}
    return out


# ── the data-model load ─────────────────────────────────────────────────────

def data_model_spec(spec_by_sheet: dict) -> dict:
    """The data-model mapper spec for an ITD delta: the admin's spec plus the
    keys every delta row carries (RESERVED_COLUMNS) — only on sheets whose
    spec already loads premium transactions, so no other sheet ever gains a
    transaction from them. A source-reference mapping the admin made is kept."""
    from data_model import DATA_MODEL, field_destination
    out = {}
    for sheet, spec in (spec_by_sheet or {}).items():
        spec = dict(spec or {})
        loads_txn = any(k in DATA_MODEL and field_destination(k)[0] == "premium_transaction"
                        for k in spec)
        if loads_txn:
            spec["premium_transaction_row_key"] = ROW_KEY_COL
            spec["premium_transaction_itd_adjusts_row_key"] = ADJUSTS_COL
            spec["premium_transaction_itd_reverses_row_key"] = REVERSES_COL
            spec.setdefault("premium_transaction_source_ref", SOURCE_REF_COL)
        out[sheet] = spec
    return out


# ── suggesting a config ─────────────────────────────────────────────────────

_KEY_HINTS = ("invoice number", "invoice no", "invoice ref", "transaction id",
              "transaction reference", "transaction ref", "transaction number",
              "bordereau reference", "unique reference", "line reference")
_PERIOD_HINTS = ("reporting end date", "reporting period end", "reported period end",
                 "bordereau period end", "period end", "reporting period",
                 "reporting month", "bordereau month", "reporting start date")
_TXN_DATE_HINTS = ("effective date of transaction", "transaction effective date",
                   "transaction date", "endorsement effective date",
                   "booking date", "invoice date")
_MONEY_HINTS = ("premium", "commission amount", "commission $", "brokerage amount",
                "fee", "tax", "levy", "net ", "payable", "amount", "$",
                # Risksmith: "Risksmith Retained Revenue", "Retained Commisson +
                # Fronting" (sic) — amounts whose headings name none of the above.
                "revenue", "retained", "commisson")
_NOT_MONEY = ("percent", " rate", "limit", "attachment", "excess of", "sum insured",
              "date", "number", "no.", "code", "name", "currency", "part of",
              "% written", "type", "indicator", "status")


def _is_money_heading(n: str) -> bool:
    """A normalised heading that names an amount. '100% Gross Written Premium'
    is money (a share, then an amount); 'Broker Commission%' is a rate."""
    if n.endswith("%"):
        return False
    return (any(h in n for h in _MONEY_HINTS)
            and not any(x in n for x in _NOT_MONEY))


# Two sheets with the same transaction id column whose headings agree at least
# this much carry the same transactions — a workbook's monthly tab and its ITD
# tab (Risksmith's share 90 of 91 headings). A premium tab and a claims tab
# that happen to share an id column name agree far less, and stay apart.
SAME_TRANSACTIONS_OVERLAP = 0.9


def _share_streams(sheets: dict[str, dict], layout: list[tuple[str, list[str]]]) -> None:
    """Give sheets that carry the same transactions ONE stream — one running
    total — so a transaction on both the monthly tab and the ITD tab of a
    workbook is counted once (the second copy is a duplicate in the file).

    They are compared only on the headings every one of them has: the
    Risksmith tabs name one column differently ("Payable due AmWins Re" /
    "... Palms Net Written Premium $"), and a heading only one tab has would
    make every transaction delivered from the other tab look changed. Edits
    `sheets` in place."""
    cols = {name: {_norm_name(c): c for c in cs} for name, cs in layout or []}
    groups: list[list[str]] = []
    for name, sc in sheets.items():
        key = _identity(sc)
        if key is None or name not in cols:
            continue
        for g in groups:
            a, b = set(cols[g[0]]), set(cols[name])
            if (_identity(sheets[g[0]]) == key
                    and len(a & b) / max(len(a), len(b), 1) >= SAME_TRANSACTIONS_OVERLAP):
                g.append(name)
                break
        else:
            groups.append([name])
    for g in groups:
        if len(g) < 2:
            continue
        stream = " + ".join(g)
        common = set.intersection(*(set(cols[n]) for n in g))
        for n in g:
            sc = sheets[n]
            sc["stream"] = stream
            sc["money_columns"] = [c for c in sc["money_columns"] if _norm_name(c) in common]
            sc["ignore_columns"] = list(dict.fromkeys(
                [*sc["ignore_columns"], *(c for nk, c in cols[n].items() if nk not in common)]))


def suggest_config(layout: list[tuple[str, list[str]]]) -> dict:
    """A first itd_config from a setup's input sheets and headings (GET
    /itd/pipelines/{id}/config returns it). Mode stays 'never': a suggestion is
    about how to read the file, not whether to. auto_config() is the stricter
    version a new setup is switched on with."""
    sheets = _suggest_sheets(layout)
    _share_streams(sheets, layout)
    return {"mode": "never", "on_changed": "adjust", "adjust_suffix": "-ADJ",
            "sheets": sheets}


def _whole_business_key(sc: dict) -> bool:
    """All four columns of the business key were found: policy, transaction
    type, transaction date and reported period."""
    return len(sc.get("fallback_key_columns") or []) == 4 and bool(sc.get("period_column"))


def _identity(sc: dict) -> Optional[tuple[str, list[str]]]:
    """What a sheet's rows are matched on: its id column, else its business key."""
    if sc.get("key_columns"):
        return "txn", [_norm_name(c) for c in sc["key_columns"]]
    if sc.get("fallback_key_columns"):
        return "cmp", [_norm_name(c) for c in sc["fallback_key_columns"]]
    return None


def _suggest_sheets(layout: list[tuple[str, list[str]]]) -> dict[str, dict]:
    """Each sheet's settings on its own, read from its headings."""
    sheets: dict[str, dict] = {}
    for sheet, cols in layout or []:
        lc = {c: _norm_name(c) for c in cols}

        def first(hints):
            for h in hints:
                for c, n in lc.items():
                    if h in n:
                        return c
            return None

        key = first(_KEY_HINTS)
        policy = first(("policy number", "policy no", "policy reference",
                        "certificate number"))
        txn_type = first(("transaction type",))
        txn_date = first(_TXN_DATE_HINTS)
        period = first(_PERIOD_HINTS)
        period_start = first(("reporting start date", "period start"))
        money = [c for c, n in lc.items() if _is_money_heading(n)]
        # The client's business key (Open Questions Register): policy,
        # transaction type, effective date and reported period. The amount is
        # compared, never part of the key — see the module docstring.
        fallback = [c for c in (policy, txn_type, txn_date, period or period_start) if c]
        if not key and not fallback:
            continue
        sheets[sheet] = {
            "stream": sheet,
            "key_columns": [key] if key else [],
            "fallback_key_columns": list(dict.fromkeys(fallback)),
            "period_column": period,
            "transaction_date_column": txn_date,
            "money_columns": money,
            "ignore_columns": [],
        }
    return sheets


def auto_config(layout: list[tuple[str, list[str]]]) -> Optional[dict]:
    """The ITD settings a NEW setup starts with, from its input sheets and
    headings — so nobody has to fill the ITD tab in by hand.

    Only sheets with a reported-period column, and a way to match a row with
    what was delivered before, are kept: a transaction id column, or — for a
    broker who sends none — the whole business key (policy, transaction type,
    transaction date, reported period). Without the period a file cannot be
    told apart from a monthly one. None (ITD off, exactly today's behaviour)
    when no sheet qualifies. Otherwise mode 'auto' — each file is still only
    treated as ITD when its rows span more than one period, so a broker who
    sends monthly files sees no change."""
    sheets = {name: sc for name, sc in _suggest_sheets(layout).items()
              if sc["period_column"] and (sc["key_columns"] or _whole_business_key(sc))}
    if not sheets:
        return None
    _share_streams(sheets, layout)
    return normalize_config({"mode": "auto", "on_changed": "adjust",
                             "adjust_suffix": "-ADJ", "sheets": sheets,
                             "auto_filled": True})
