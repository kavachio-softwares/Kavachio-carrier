"""
version_diff.py
───────────────
Previous Submission Reconciliation (plan tasks 23.2–23.4): this version of a
submission against the last ACCEPTED version of the same period.

  23.2  Rows are paired on the stable key (row_key.py) and each comes out as
        one of the task's four outcomes, or unchanged:
          new           only in this version
          changed       paired; only non-money fields differ
          disappeared   only in the accepted version (not a cancellation —
                        nothing is reversed automatically)
          restated      paired; an amount differs (gross premium, commission %,
                        net premium and every other money column), compared
                        separately from the key
          unchanged     paired; nothing compared differs
        Every row gets one of these (decided 16 Oct 2026: no "needs review"
        and no "not compared" rows). A row with no policy number or no
        transaction effective date has no key: it is unchanged when the other
        version has an identical row, else new / disappeared.

  23.3  A silent restatement told apart from a genuine endorsement
        (transaction types read as ingest reads them — ingester's own list):
          - a NEW row of type endorsement is a genuine endorsement;
          - a RESTATED row is
              corrected_on_request  its new amount is a fix recorded on an
                                    earlier version (Fix & Validate / secure
                                    link) — the broker did what was asked;
              declared              a new cancellation / flat cancellation
                                    row on the same policy explains it;
              in_place_policy_level the sheet has no transaction-type column,
                                    so a policy-level file cannot tell an
                                    endorsement from a restatement;
              silent                none of these: an accepted transaction's
                                    figures were changed in place.
          - a restatement next to a new endorsement on the same policy keeps
            its label and is flagged "check for double count".

  23.4  The sheet, the CSV and the API report these same outcomes
        (OUTCOME_WORDS).

Pairing inside a group of rows that share one key: rows equal in every
compared field first, then rows with the same amounts (same sign), then the
most similar rows (fewest values that differ, then nearest in the file) —
flagged "paired by closest match" and then classified like any other pair
(this replaced D15's "needs review" on 16 Oct 2026). Rows left over when the
two versions have different numbers of them are new / disappeared. A row
left unmatched because an OPTIONAL key part (type, period) is blank on one
side is retried without that part, and paired only when exactly one
candidate exists on each side ("matched on reduced key").

Information only: nothing here writes anywhere. No database and no web
imports, so it can be tested on its own.
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Optional

import itd
import row_key as rk
from data_model import DATA_MODEL

ENGINE_VERSION = "23.4-4"

UNCHANGED, CHANGED, RESTATED = "unchanged", "changed", "restated"
NEW, DISAPPEARED = "new", "disappeared"
# The task's order: what is new, changed, disappeared, restated.
OUTCOMES = (NEW, CHANGED, DISAPPEARED, RESTATED, UNCHANGED)
OUTCOME_WORDS = {
    NEW: "New",
    CHANGED: "Changed",
    DISAPPEARED: "Disappeared",
    RESTATED: "Restated",
    UNCHANGED: "Unchanged",
}

# 23.3 — how a restatement came about.
SILENT, DECLARED, CORRECTED, POLICY_LEVEL = (
    "silent", "declared", "corrected_on_request", "in_place_policy_level")
RESTATEMENT_WORDS = {
    SILENT: "Silent restatement",
    DECLARED: "Declared restatement",
    CORRECTED: "Corrected on request",
    POLICY_LEVEL: "Amount changed (no transaction type)",
}
# (one, many) for the plain-words line.
_RESTATEMENT_COUNT = {
    SILENT: ("silent restatement", "silent restatements"),
    DECLARED: ("declared restatement", "declared restatements"),
    CORRECTED: ("correction asked for earlier", "corrections asked for earlier"),
    POLICY_LEVEL: ("amount change without a transaction type",
                   "amount changes without a transaction type"),
}

# Transaction types are read the way ingest reads them: the system's own
# code list (ingester._TXN_TYPE_MAP via ingester._normalise_enum — exact
# code, then a code followed by more words, e.g. "Endorsement - Extension").
# A value outside that list is "other"; a blank one is the system default.
GENUINE = "endorsement"
# New rows of these types explain a change to an accepted row on the same
# policy (a declared restatement).
_EXPLAINS = ("cancellation", "flat_cancellation")
OTHER = "other"

# Data-model fields that are amounts: every decimal field of a premium
# transaction (data_model.DATA_MODEL). A field ending _percent or _rate is a
# rate — compared, never totalled.
MONEY_FIELDS = frozenset(k for k, v in DATA_MODEL.items()
                         if v.get("table") == "premium_transaction" and v.get("type") == "decimal")

# Columns other code adds to a landing; never compared.
_NEVER_COMPARED = {rk.heading(c) for c in (
    *getattr(itd, "RESERVED_COLUMNS", ()), getattr(itd, "_SOURCE_SHEET_KEY", ""))} - {""}

_CENT = Decimal("0.01")
_MICRO = Decimal("0.000001")


def _type_codes() -> tuple[dict, set, str]:
    from bdx_defaults import DEFAULT_TRANSACTION_TYPE
    from ingester import _TXN_TYPE_MAP, _normalise_enum
    return _normalise_enum, set(_TXN_TYPE_MAP.values()), DEFAULT_TRANSACTION_TYPE


def type_family(value: Any) -> str:
    """A transaction type as the system reads it at ingest: one of its own
    values (new, renewal, endorsement, cancellation, flat_cancellation, …),
    "other" when the code is not in the system's list, and the system
    default ('unknown') when blank."""
    normalise, known, default = _type_codes()
    if itd._blank(value):
        return default
    got = normalise("premium_transaction", "premium_transaction_type", str(value))
    return got if got in known else OTHER


def type_label(kind: str) -> str:
    """A system transaction-type value as words: 'flat_cancellation' → 'flat cancellation'."""
    return str(kind or "").replace("_", " ")


# ── column roles ────────────────────────────────────────────────────────────

def _is_rate(col: str, canonical: Optional[str]) -> bool:
    """A rate, by its data-model field: …_percent or …_rate."""
    return (canonical or "").endswith(("_percent", "_rate"))


def is_money(col: str, canonical: Optional[str] = None) -> bool:
    """Is this column an amount (or a rate such as the commission %)? Only by
    the data-model field the setup maps it to."""
    return (canonical or "").strip() in MONEY_FIELDS


def _money(v: Any, rate: bool) -> Decimal:
    n = itd.to_number(v)
    if n is None:
        return Decimal(0)
    return Decimal(str(n)).quantize(_MICRO if rate else _CENT, rounding=ROUND_HALF_UP)


def _same_value(a: Any, b: Any) -> bool:
    """Two non-money cells hold the same value: blank = blank, 98000.0 = '98000',
    '6,185.60' = 6185.6, 2025-08-15 = '15/08/2025'."""
    na, nb = itd.norm_value(a), itd.norm_value(b)
    if na == nb:
        return True
    if na is None or nb is None:
        return False
    xa, xb = itd.to_number(a), itd.to_number(b)
    if xa is not None and xb is not None:
        return abs(xa - xb) < 1e-9
    da, db = rk.clean_date(a), rk.clean_date(b)
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    return bool(da and db and iso.match(da) and iso.match(db) and da == db)


def _plain(v: Any) -> Any:
    """A cell as it can be shown or stored (dates as ISO text)."""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


# ── one sheet ───────────────────────────────────────────────────────────────

class _Side:
    """One version of one sheet: its key columns and its rows, sorted into
    eligible rows (both mandatory parts present), rows without a key (no
    policy number or no transaction effective date) and repeated heading
    lines that are skipped."""

    def __init__(self, sheet: dict, key_cols: dict, parts: list[str], formats: dict):
        self.columns = list(sheet.get("columns") or [])
        self.key_cols = key_cols
        self.eligible: list[dict] = []
        self.no_key: list[dict] = []
        self.skipped = 0
        for i, row in enumerate(sheet.get("rows") or []):
            if not isinstance(row, dict):
                continue
            if rk.is_heading_repeat(row, key_cols):
                self.skipped += 1
                continue
            values = rk.row_values(row, key_cols, rk.PARTS, formats)
            entry = {"i": i, "row": row, "values": values,
                     "key": rk.key_of(values, parts), "paired": False}
            missing = rk.missing_mandatory(values)
            if missing:
                entry["missing"] = missing
                self.no_key.append(entry)
            else:
                self.eligible.append(entry)


def _sheet_pairs(base: dict, cur: dict) -> list[tuple[Optional[str], Optional[str], Optional[str]]]:
    """(baseline sheet, current sheet, note) for every sheet of either version,
    matched by name (case and spacing forgiven). When neither shares a name
    and each has exactly one sheet, the two are compared ('sheet renamed')."""
    bn = {rk.heading(n): n for n in base}
    cn = {rk.heading(n): n for n in cur}
    common = [k for k in cn if k in bn]
    if not common and len(bn) == 1 and len(cn) == 1:
        return [(next(iter(bn.values())), next(iter(cn.values())), "sheet renamed")]
    out = [(bn[k], cn[k], None) for k in common]
    out += [(None, cn[k], None) for k in cn if k not in bn]
    out += [(bn[k], None, None) for k in bn if k not in cn]
    return out


def _signature(entry: dict, cols: list[str], money: dict) -> tuple:
    row = entry["row"]
    out = []
    for c in cols:
        if c in money:
            out.append(str(_money(row.get(c), money[c])))
        else:
            out.append(itd.norm_value(row.get(c)) or "")
    return tuple(out)


def _amounts(entry: dict, money: dict) -> tuple:
    return tuple(str(_money(entry["row"].get(c), r)) for c, r in money.items())


def _sign(entry: dict, money: dict) -> int:
    total = sum((_money(entry["row"].get(c), r) for c, r in money.items() if not r),
                Decimal(0))
    return -1 if total < 0 else 1


def _pair_group(bl: list[dict], cl: list[dict], cols: list[str], money: dict
                ) -> list[tuple[dict, dict, str]]:
    """Pairs inside one key group: identical rows, then rows with the same
    amounts, then the most similar rows ("closest"). Rows left over are
    unpaired (new / disappeared)."""
    if len(bl) == 1 and len(cl) == 1:
        return [(bl[0], cl[0], "key")]
    pairs: list[tuple[dict, dict, str]] = []
    b_left, c_left = list(bl), list(cl)

    def match(test, how):
        nonlocal b_left, c_left
        for c in list(c_left):
            b = next((b for b in b_left if test(b, c)), None)
            if b is not None:
                pairs.append((b, c, how))
                b_left.remove(b)
                c_left.remove(c)

    match(lambda b, c: _signature(b, cols, money) == _signature(c, cols, money), "exact")
    if money:
        match(lambda b, c: (_amounts(b, money) == _amounts(c, money)
                            and _sign(b, money) == _sign(c, money)), "same_amount")
    # The rest: most similar first — fewest compared values that differ, then
    # nearest in the file — within the same sign (a refund is never paired
    # with a premium).
    sig = {id(e): _signature(e, cols, money) for e in (*b_left, *c_left)}
    for sign in (1, -1):
        bs = [b for b in b_left if not money or _sign(b, money) == sign]
        cs = [c for c in c_left if not money or _sign(c, money) == sign]
        ranked = sorted(
            (sum(x != y for x, y in zip(sig[id(b)], sig[id(c)])), abs(b["i"] - c["i"]), bi, ci)
            for bi, b in enumerate(bs) for ci, c in enumerate(cs))
        used_b, used_c = set(), set()
        for _diff, _gap, bi, ci in ranked:
            if bi in used_b or ci in used_c:
                continue
            used_b.add(bi)
            used_c.add(ci)
            pairs.append((bs[bi], cs[ci], "closest"))
            b_left.remove(bs[bi])
            c_left.remove(cs[ci])
    return pairs


def _reduced_retry(b_left: list[dict], c_left: list[dict], parts: list[str]
                   ) -> list[tuple[dict, dict, str]]:
    """Pair rows still unmatched because an optional key part is blank on one
    side: same policy and date, every optional part equal or blank on either
    side — and exactly one candidate each way."""
    opt = [p for p in parts if p in rk.OPTIONAL]
    if not opt:
        return []

    def fits(b, c):
        bv, cv = b["values"], c["values"]
        if any(bv.get(p) != cv.get(p) for p in rk.MANDATORY):
            return False
        blank = False
        for p in opt:
            if not bv.get(p) or not cv.get(p):
                blank = True
            elif bv.get(p) != cv.get(p):
                return False
        return blank

    core = defaultdict(list)
    for b in b_left:
        core[(b["values"][rk.POLICY], b["values"][rk.DATE])].append(b)
    pairs = []
    for c in c_left:
        cands = [b for b in core.get((c["values"][rk.POLICY], c["values"][rk.DATE]), [])
                 if not b["paired"] and fits(b, c)]
        if len(cands) != 1:
            continue
        b = cands[0]
        back = [x for x in c_left if not x["paired"] and fits(b, x)]
        if len(back) != 1:
            continue
        b["paired"] = c["paired"] = True
        pairs.append((b, c, "reduced_key"))
    return pairs


def _row_no(entry: Optional[dict]) -> Optional[int]:
    return entry["i"] + 1 if entry else None


def _base_item(sheet: str, entry: dict, parts: list[str], side_cols: dict,
               name_col: Optional[str] = None) -> dict:
    v = entry["values"]
    return {
        "sheet": sheet,
        "key": rk.key_text(v, parts),
        "policy": entry["row"].get(side_cols.get(rk.POLICY)) if side_cols.get(rk.POLICY) else None,
        "insured": _plain(entry["row"].get(name_col)) if name_col else None,
        "type": entry["row"].get(side_cols.get(rk.TYPE)) if side_cols.get(rk.TYPE) else None,
        "effective_date": v.get(rk.DATE),
        "period": v.get(rk.PERIOD),
        "flags": [],
        "note": None,
    }


def _compare_pair(b: dict, c: dict, cols: list[str], money: dict) -> tuple[dict, list[str], dict]:
    """(money moved {col: {was, now, change}}, other changed columns, {col: {was, now}})."""
    moved = {}
    for col, rate in money.items():
        was, now = _money(b["row"].get(col), rate), _money(c["row"].get(col), rate)
        if was != now:
            moved[col] = {"was": float(was), "now": float(now), "change": float(now - was)}
    changed, detail = [], {}
    for col in cols:
        if col in money:
            continue
        if not _same_value(b["row"].get(col), c["row"].get(col)):
            changed.append(col)
            detail[col] = {"was": _plain(b["row"].get(col)), "now": _plain(c["row"].get(col))}
    return moved, changed, detail


def _compare_sheet(name: str, bsheet: dict, csheet: dict, bset: dict, cset: dict,
                   corrections: list[dict], note: Optional[str]) -> dict:
    bres = rk.resolve_key_columns(bsheet.get("columns") or [], bset.get("canonical_of"),
                                  bset.get("period_column"))
    cres = rk.resolve_key_columns(csheet.get("columns") or [], cset.get("canonical_of"),
                                  cset.get("period_column"))
    bcols, ccols = bres["columns"], cres["columns"]
    parts = rk.parts_for(bcols, ccols)
    info = {
        "sheet": name, "note": note,
        "key_columns": {"accepted": bcols, "this": ccols},
        "found_by": {"accepted": bres["found_by"], "this": cres["found_by"]},
        "parts_used": parts or [],
        "rows": {"accepted": len(bsheet.get("rows") or []),
                 "this": len(csheet.get("rows") or [])},
    }
    bcolset, ccolset = list(bsheet.get("columns") or []), list(csheet.get("columns") or [])
    info["columns_added"] = [c for c in ccolset if rk.heading(c) not in {rk.heading(x) for x in bcolset}]
    info["columns_removed"] = [c for c in bcolset if rk.heading(c) not in {rk.heading(x) for x in ccolset}]
    if parts is None:
        gaps = []
        for p in rk.MANDATORY:
            where = [w for w, cols in (("the accepted version", bcols), ("this version", ccols))
                     if not cols.get(p)]
            if where:
                gaps.append(f"{rk.PART_WORDS[p].lower()} ({' and '.join(where)})")
        info["state"] = "key_column_missing"
        info["state_text"] = ("Not compared — the setup maps no column to " + "; ".join(gaps)
                              + ". Map it in Bordereau Setup.")
        return {"info": info, "items": [], "unchanged": 0, "money": {}}

    bside = _Side(bsheet, bcols, parts, bset.get("date_formats") or {})
    cside = _Side(csheet, ccols, parts, cset.get("date_formats") or {})
    info["state"] = "compared"
    info["skipped_lines"] = {"accepted": bside.skipped, "this": cside.skipped}
    key_heads = {rk.heading(c) for c in (*bcols.values(), *ccols.values()) if c}
    canon = {**(bset.get("canonical_of") or {}), **(cset.get("canonical_of") or {})}
    common = [c for c in ccolset if rk.heading(c) in {rk.heading(x) for x in bcolset}]
    bname = {rk.heading(x): x for x in bcolset}
    mapped = cset.get("mapped_columns")
    compared, money = [], {}
    for c in common:
        h = rk.heading(c)
        if h in key_heads or h in _NEVER_COMPARED:
            continue
        if mapped is not None and h not in mapped:     # only what the setup reads
            continue
        if bname[h] != c:              # same column, spelt differently: read both
            for e in (*bside.eligible, *bside.no_key):
                e["row"] = {**e["row"], c: e["row"].get(bname[h])}
        if is_money(c, canon.get(c)):
            money[c] = _is_rate(c, canon.get(c))
        compared.append(c)
    info["money_columns"] = list(money)
    info["money_fields"] = {c: canon.get(c) for c in money}
    info["compared_columns"] = len(compared)
    taken = [c for c in (*bcols.values(), *ccols.values()) if c]
    bname_col = rk.name_column(bcolset, bset.get("canonical_of"), taken)
    cname_col = rk.name_column(ccolset, cset.get("canonical_of"), taken)
    info["name_column"] = {"accepted": bname_col, "this": cname_col}

    groups_b, groups_c = defaultdict(list), defaultdict(list)
    for e in bside.eligible:
        groups_b[e["key"]].append(e)
    for e in cside.eligible:
        groups_c[e["key"]].append(e)

    pairs: list[tuple[dict, dict, str, bool]] = []
    for key in dict.fromkeys([*groups_c, *groups_b]):
        bl, cl = groups_b.get(key, []), groups_c.get(key, [])
        if not bl or not cl:
            for e in (bl if len(bl) > 1 else []) + (cl if len(cl) > 1 else []):
                e["dup"] = True
            continue
        dup = len(bl) > 1 or len(cl) > 1
        for b, c, how in _pair_group(bl, cl, compared, money):
            b["paired"] = c["paired"] = True
            pairs.append((b, c, how, dup))
        if dup:
            for e in [*bl, *cl]:
                if not e["paired"]:
                    e["dup"] = True

    b_left = [e for e in bside.eligible if not e["paired"]]
    c_left = [e for e in cside.eligible if not e["paired"]]
    for b, c, how in _reduced_retry(b_left, c_left, parts):
        pairs.append((b, c, how, False))

    items: list[dict] = []
    unchanged = 0
    matched: list[tuple[int, int]] = []        # (this row, accepted row), 0-based
    totals = {c: {"accepted": Decimal(0), "this": Decimal(0), "restated": Decimal(0),
                  "new": Decimal(0), "disappeared": Decimal(0)}
              for c, r in money.items() if not r}

    for side_key, sd in (("accepted", bside), ("this", cside)):
        for e in (*sd.eligible, *sd.no_key):
            for c in totals:
                totals[c][side_key] += _money(e["row"].get(c), False)

    # A row with no key is the same row only when the other version has one
    # identical to it: the key parts it does have, and every compared value.
    def same_row(e):
        return (tuple(e["values"].get(p) or "" for p in rk.PARTS),
                _signature(e, compared, money))
    waiting = defaultdict(list)
    for e in bside.no_key:
        waiting[same_row(e)].append(e)
    for e in cside.no_key:
        twins = waiting.get(same_row(e))
        if twins:
            b = twins.pop(0)
            b["paired"] = e["paired"] = True
            unchanged += 1
            matched.append((e["i"], b["i"]))

    for b, c, how, dup in pairs:
        moved, changed, detail = _compare_pair(b, c, compared, money)
        if moved:
            outcome = RESTATED
        elif changed:
            outcome = CHANGED
        else:
            outcome = UNCHANGED
        for col in totals:
            delta = _money(c["row"].get(col), False) - _money(b["row"].get(col), False)
            totals[col]["restated"] += delta
        if outcome == UNCHANGED and how in ("key", "exact"):
            unchanged += 1
            matched.append((c["i"], b["i"]))
            continue
        it = _base_item(name, c, parts, ccols, cname_col)
        it.update(outcome=outcome, row=_row_no(c), accepted_row=_row_no(b), paired_by=how,
                  money=moved, changed_fields=changed, changes=detail)
        if how == "reduced_key":
            it["flags"].append("matched_on_reduced_key")
        if how == "closest":
            it["flags"].append("paired_by_closest_match")
        elif dup:
            it["flags"].append("duplicate_key")
        if moved:
            if all(m["now"] == 0 for m in moved.values()):
                it["flags"].append("restated_to_zero")
            if any((m["was"] < 0) != (m["now"] < 0) and m["was"] and m["now"]
                   for m in moved.values()):
                it["flags"].append("sign_flipped")
        it["_entry"] = c
        it["_base"] = b
        items.append(it)

    def no_key(it: dict, e: dict) -> None:
        if e.get("missing"):
            it["missing"] = [rk.PART_WORDS[p] for p in e["missing"]]
            it["flags"].append("no_key")

    for e in (*cside.eligible, *cside.no_key):
        if e["paired"]:
            continue
        it = _base_item(name, e, parts, ccols, cname_col)
        no_key(it, e)
        it.update(outcome=NEW, row=_row_no(e), accepted_row=None, paired_by=None,
                  money={c: {"was": None, "now": float(_money(e["row"].get(c), r)),
                             "change": float(_money(e["row"].get(c), r))}
                         for c, r in money.items() if _money(e["row"].get(c), r)},
                  changed_fields=[], changes={})
        if e.get("dup"):
            it["flags"].append("duplicate_key")
        for col in totals:
            totals[col]["new"] += _money(e["row"].get(col), False)
        it["_entry"] = e
        items.append(it)

    for e in (*bside.eligible, *bside.no_key):
        if e["paired"]:
            continue
        it = _base_item(name, e, parts, bcols, bname_col)
        no_key(it, e)
        it.update(outcome=DISAPPEARED, row=None, accepted_row=_row_no(e), paired_by=None,
                  money={c: {"was": float(_money(e["row"].get(c), r)), "now": None,
                             "change": -float(_money(e["row"].get(c), r))}
                         for c, r in money.items() if _money(e["row"].get(c), r)},
                  changed_fields=[], changes={})
        if e.get("dup"):
            it["flags"].append("duplicate_key")
        for col in totals:
            totals[col]["disappeared"] += _money(e["row"].get(col), False)
        it["_entry"] = e
        items.append(it)

    _classify(items, parts, corrections, name)
    money_out = {c: {k: float(v) for k, v in t.items()} for c, t in totals.items()}
    for c, t in money_out.items():
        gap = round(t["this"] - (t["accepted"] + t["restated"]
                                 + t["new"] - t["disappeared"]), 2)
        t["control_gap"] = gap
    # What the side-by-side view needs: both versions' rows (references, not
    # copies), the rows paired as unchanged, and the columns the setup reads
    # in this file's order (key, insured, compared).
    shown = {rk.heading(c) for c in (*ccols.values(), cname_col, *compared) if c}
    view_cols = [c for c in ccolset if rk.heading(c) in shown]
    view = {"columns": view_cols,
            # columns the setup maps to a data-model date field: shown as dates
            "date_columns": [c for c in view_cols if (DATA_MODEL.get(
                rk._field(canon.get(c)) or "") or {}).get("type") in ("date", "datetime")],
            "accepted_column": {c: bname.get(rk.heading(c)) for c in ccolset},
            "this_rows": csheet.get("rows") or [], "accepted_rows": bsheet.get("rows") or [],
            "matched": matched}
    return {"info": info, "items": items, "unchanged": unchanged, "money": money_out,
            "view": view}


def _classify(items: list[dict], parts: list[str], corrections: list[dict], sheet: str) -> None:
    """23.3 — new rows get their family; restated rows get how they came about;
    then a one-line detail for people."""
    has_type = rk.TYPE in parts
    new_by_policy = defaultdict(list)
    for it in items:
        if it["outcome"] == NEW:
            it["kind"] = type_family(it.get("type") if has_type else None)
            pol = it["_entry"]["values"].get(rk.POLICY)
            if pol:                     # a row with no policy number explains nothing
                new_by_policy[pol].append(it)
    fixes = [f for f in corrections or [] if rk.heading(f.get("sheet")) == rk.heading(sheet)]

    for it in items:
        o = it["outcome"]
        e = it.get("_entry")
        pol = e["values"].get(rk.POLICY) if e else None
        if o == RESTATED:
            same_pol = [n for n in new_by_policy.get(pol, [])]
            explains = [n for n in same_pol if n.get("kind") in _EXPLAINS
                        and (not n["_entry"]["values"].get(rk.DATE)
                             or n["_entry"]["values"].get(rk.DATE) == e["values"].get(rk.DATE))]
            if not has_type:
                it["restatement"] = POLICY_LEVEL
            elif _corrected(it, e, fixes, parts):
                it["restatement"] = CORRECTED
            elif explains:
                it["restatement"] = DECLARED
                it["declared_by"] = [{"row": n["row"], "type": n.get("type")} for n in explains]
                for n in explains:
                    n.setdefault("explains", []).append(it["row"])
            else:
                it["restatement"] = SILENT
            if any(n.get("kind") == GENUINE for n in same_pol):
                it["flags"].append("endorsement_added_same_policy")
        elif o == DISAPPEARED and pol in new_by_policy:
            repl = [n for n in new_by_policy[pol] if n.get("kind") in _EXPLAINS]
            if repl:
                it["flags"].append("replaced_by_new_row")
                it["note"] = ("A new " + " / ".join(sorted({str(n.get("type") or n["kind"])
                                                             for n in repl}))
                              + " row on the same policy may replace it.")
    for it in items:
        it["detail"] = _detail(it)
        it.pop("_entry", None)
        it.pop("_base", None)


def _corrected(it: dict, e: dict, fixes: list[dict], parts: list[str]) -> bool:
    """Every amount that moved equals a fix recorded for this row and column on
    an earlier version (Fix & Validate or the secure link)."""
    if not fixes or not it.get("money"):
        return False
    for col, m in it["money"].items():
        ok = False
        for f in fixes:
            fv = f.get("values") or {}
            if any((fv.get(p) or "") != (e["values"].get(p) or "") for p in parts):
                continue
            if rk.heading(f.get("column")) != rk.heading(col):
                continue
            n = itd.to_number(f.get("value"))
            if n is not None and abs(n - m["now"]) < 0.005:
                ok = True
                break
        if not ok:
            return False
    return True


# What a new row is, in plain words: (detail, one, many).
_FLAG_SENTENCES = {
    "matched_on_reduced_key": "Matched although the transaction type or reported period "
                              "is blank on one side.",
    "restated_to_zero": "Its amounts are now zero.",
    "sign_flipped": "An amount changed from positive to negative or back.",
    "paired_by_closest_match": "Other rows have the same policy, transaction type and "
                               "effective date, so it was paired with the most similar one.",
    "duplicate_key": "Other rows share this policy, transaction type and effective date.",
}


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _rows_ref(rows) -> str:
    rows = [r for r in rows or [] if r]
    if not rows:
        return ""
    return (" (row " if len(rows) == 1 else " (rows ") + ", ".join(map(str, rows)) + ")"


def _detail(it: dict) -> str:
    """What happened to a row, in plain words (the sheet's "What happened")."""
    o = it["outcome"]
    flags = it.get("flags") or []
    if o == RESTATED:
        k = it.get("restatement")
        if k == DECLARED:
            by = it.get("declared_by") or []
            kinds = " / ".join(sorted({" ".join(str(d.get("type")).split()) for d in by}))
            text = (f"Amount changed, and a new {kinds} row in this file"
                    f"{_rows_ref([d.get('row') for d in by])} explains it (declared restatement).")
        elif k == CORRECTED:
            text = ("Amount changed to the fix asked for on an earlier version "
                    "(corrected on request).")
        elif k == POLICY_LEVEL:
            text = ("Amount changed. This file has no transaction type, so this can't be told "
                    "apart from an endorsement.")
        else:
            text = "Amount changed, and nothing in the file says why (silent restatement)."
        if it.get("changed_fields"):
            text += " Other details changed too."
        if "endorsement_added_same_policy" in flags:
            text += (" A new endorsement was also added for this policy: check the amount "
                     "isn't counted twice.")
    elif o == NEW:
        typ = it.get("type")
        text = ("New row with no transaction type" if itd._blank(typ)
                else f"New row, transaction type '{' '.join(str(typ).split())}'")
        if it.get("explains"):
            rows = [r for r in it["explains"] if r]
            ref = (("row " if len(rows) == 1 else "rows ") + ", ".join(map(str, rows))
                   if rows else "a changed row")
            text += f": it explains the amount change on {ref}"
        elif it.get("kind") == GENUINE:
            text += ": a genuine new transaction (endorsement)"
        text += "."
    elif o == DISAPPEARED:
        text = "Was in the accepted version, is not in this file."
        if "replaced_by_new_row" in flags and it.get("note"):
            text += " " + it["note"]
    elif o == CHANGED:
        text = "Details changed (no amounts): " + ", ".join(it.get("changed_fields") or []) + "."
    else:
        text = OUTCOME_WORDS.get(o, o) + "."
    extra = [_FLAG_SENTENCES[f] for f in flags if f in _FLAG_SENTENCES]
    if "no_key" in flags:
        missing = " and no ".join(m.lower() for m in it.get("missing") or [])
        other = "this file" if o == DISAPPEARED else "the accepted version"
        extra.insert(0, f"It has no {missing}, so it could only be matched to an identical row, "
                        f"and {other} has none.")
    return " ".join([text, *dict.fromkeys(extra)])


# ── both versions ───────────────────────────────────────────────────────────

def diff_versions(accepted: dict, current: dict, *, accepted_setup: Optional[dict] = None,
                  current_setup: Optional[dict] = None,
                  corrections: Optional[list[dict]] = None,
                  max_rows: int = 200_000) -> dict:
    """Compare two landings ({"sheets": {name: {"columns", "rows"}}}).

    `*_setup` describe how each version's setup reads its columns:
      canonical_of   {input column: data-model field} from the column mapping
      period_column  {sheet: column} (or one column for every sheet) — the
                     reported-period column named in the setup's ITD settings
      mapped_columns the input columns the setup reads; only these are
                     compared (None = every column both versions have)
      date_formats   {input column: date format} from the setup's date rules
    `corrections` are fixes recorded on versions between the two
    ({sheet, values: {part: cleaned}, column, value}).

    Returns {state, summary, sheets, rows}; rows excludes unchanged rows (they
    are counted). Never raises on odd input — a sheet it cannot read is
    listed with its state."""
    asets, csets = accepted_setup or {}, current_setup or {}
    bsheets = (accepted or {}).get("sheets") or {}
    csheets = (current or {}).get("sheets") or {}
    if (accepted or {}).get("itd") or (current or {}).get("itd"):
        return _empty("itd_file", "Not compared — an ITD file: see the ITD comparison instead.")
    n_rows = sum(len(s.get("rows") or []) for s in (*bsheets.values(), *csheets.values()))
    if n_rows > 2 * max_rows:
        return _empty("too_large", f"Not compared — {n_rows:,} rows is more than this check handles.")

    def per_sheet(sets: dict, sheet: str) -> dict:
        pc = sets.get("period_column")
        if isinstance(pc, dict):
            pc = next((v for k, v in pc.items() if rk.heading(k) == rk.heading(sheet)), None)
        mapped = sets.get("mapped_columns")
        return {"canonical_of": sets.get("canonical_of") or {}, "period_column": pc,
                "mapped_columns": {rk.heading(m) for m in mapped} if mapped else None,
                "date_formats": sets.get("date_formats") or {}}

    sheets, rows = [], []
    views: dict = {}
    unchanged = 0
    money: dict[str, dict] = {}
    for bname, cname, note in _sheet_pairs(bsheets, csheets):
        if bname is None or cname is None:
            sheets.append({"sheet": cname or bname, "state": "only_in_this" if bname is None
                           else "only_in_accepted",
                           "state_text": ("Only in this version" if bname is None
                                          else "Only in the accepted version"),
                           "parts_used": []})
            continue
        try:
            res = _compare_sheet(cname, bsheets[bname], csheets[cname],
                                 per_sheet(asets, bname), per_sheet(csets, cname),
                                 corrections or [], note)
        except Exception as e:  # noqa: BLE001 — one odd sheet never costs the rest
            sheets.append({"sheet": cname, "state": "error", "state_text": f"Not compared — {e}",
                           "parts_used": []})
            continue
        sheets.append(res["info"])
        rows.extend(res["items"])
        if res.get("view"):
            views[cname] = res["view"]
        unchanged += res["unchanged"]
        for col, t in res["money"].items():
            acc = money.setdefault(col, {k: 0.0 for k in t})
            for k, v in t.items():
                acc[k] = round(acc[k] + v, 6)

    compared = [s for s in sheets if s.get("state") == "compared"]
    summary = _summary(rows, unchanged, money, sheets)
    if not compared:
        state = "not_compared"
    else:
        state = "compared"
    summary["plain"] = _plain_words(state, summary, sheets)
    order = {o: i for i, o in enumerate(OUTCOMES)}
    rows.sort(key=lambda r: (order.get(r["outcome"], 99), str(r["sheet"]),
                             r.get("row") or 0, r.get("accepted_row") or 0))
    return {"state": state, "engine": ENGINE_VERSION, "summary": summary,
            "sheets": sheets, "rows": rows, "view": views}


def _empty(state: str, text: str) -> dict:
    s = _summary([], 0, {}, [])
    s["plain"] = text
    return {"state": state, "engine": ENGINE_VERSION, "summary": s, "sheets": [], "rows": []}


def _summary(rows: list[dict], unchanged: int, money: dict, sheets: list[dict]) -> dict:
    by = {o: 0 for o in OUTCOMES}
    by[UNCHANGED] = unchanged
    rest = defaultdict(int)
    kinds = defaultdict(int)
    for r in rows:
        by[r["outcome"]] += 1
        if r["outcome"] == RESTATED:
            rest[r.get("restatement") or SILENT] += 1
        if r["outcome"] == NEW:
            kinds[r.get("kind") or OTHER] += 1
    return {
        "outcomes": by,
        "restatements": dict(rest),
        "new_kinds": dict(kinds),
        "genuine_endorsements": kinds.get(GENUINE, 0),
        "silent_restatements": rest.get(SILENT, 0),
        "money": money,
        "sheets_compared": len([s for s in sheets if s.get("state") == "compared"]),
    }


def restated_parts(s: dict) -> list[str]:
    """How the restated rows came about: ['2 silent restatements', '1 declared restatement']."""
    return [_n(n, *_RESTATEMENT_COUNT[k]) for k, n in (s.get("restatements") or {}).items()
            if n and k in _RESTATEMENT_COUNT]


def new_parts(s: dict) -> list[str]:
    """What the new rows are, by the system's transaction types:
    ['1 endorsement', '1 new'] (most first)."""
    _normalise, _known, default = _type_codes()
    out = []
    for k, n in sorted((s.get("new_kinds") or {}).items(), key=lambda x: (-x[1], str(x[0]))):
        label = ("without a transaction type" if k == default
                 else "of another transaction type" if k == OTHER else type_label(k))
        out.append(f"{n} {label}")
    return out


def _plain_words(state: str, s: dict, sheets: list[dict]) -> str:
    """'3 new (by type: …), 1 changed, 2 disappeared, 2 restated (2 silent
    restatements), 5 unchanged.' — the task's order."""
    if state != "compared":
        why = next((x.get("state_text") for x in sheets if x.get("state_text")), None)
        return why or "Not compared — no sheet could be matched on the key."
    o = s["outcomes"]
    if not any(o[k] for k in OUTCOMES if k != UNCHANGED):
        return (f"All {o[UNCHANGED]:,} rows are the same as in the accepted version."
                if o[UNCHANGED] != 1 else "The 1 row is the same as in the accepted version.")
    bits = []
    if o[NEW]:
        bits.append(f"{o[NEW]} new (by type: {', '.join(new_parts(s))})")
    if o[CHANGED]:
        bits.append(f"{o[CHANGED]} changed")
    if o[DISAPPEARED]:
        bits.append(f"{o[DISAPPEARED]} disappeared")
    if o[RESTATED]:
        bits.append(f"{o[RESTATED]} restated ({', '.join(restated_parts(s))})")
    bits.append(f"{o[UNCHANGED]} unchanged")
    return ", ".join(bits) + "."
