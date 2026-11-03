"""ITD handling — itd.py (pure) and itd_store.py (on an in-memory SQLite DB).

Run:  pytest test_itd.py      (no network, no shared database)

The scenario tests follow the requirement's own example: a July ITD of 70
rows; an August ITD of 89 rows where 50 are unchanged, 20 changed money and
19 are new — so 39 rows go on, not 89. test_risksmith_sample_* run the same
on the real Risksmith Schedule H workbook when it is on disk (skipped if not).
"""
from __future__ import annotations

import copy
import os
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

import itd
import itd_store

KEY = "Invoice Number"
POL = "Policy Number"
TYPE = "Transaction Type"
EFF = "Effective Date"
PSTART = "Reporting Start Date"
PEND = "Reporting End Date"
GWP = "Gross Premium"
COMM = "Commission Amount"
INSURED = "Insured Name"

SHEET = "ITD"

CFG = itd.normalize_config({
    "mode": "auto",
    "sheets": {SHEET: {
        "key_columns": [KEY],
        "fallback_key_columns": [POL, TYPE, EFF, PSTART],
        "period_column": PEND,
        "transaction_date_column": EFF,
        "money_columns": [GWP, COMM],
    }},
})


def _row(i: int, period: str = "2025-07", gwp: float = 1000.0, *, key=True,
         insured: str = "ACME") -> dict:
    y, m = period.split("-")
    return {
        KEY: f"INV{100000 + i}" if key else None,
        POL: f"POL-{i:04d}", TYPE: "New Business Premium",
        EFF: f"{period}-01T00:00:00", PSTART: f"{period}-01T00:00:00",
        PEND: f"{period}-28T00:00:00", GWP: gwp, COMM: round(gwp * 0.15, 2),
        INSURED: insured,
    }


def _landing(rows: list[dict], sheet: str = SHEET) -> dict:
    cols = list(rows[0].keys()) if rows else []
    return {"sheets": {sheet: {"columns": cols, "rows": rows}}, "row_count": len(rows)}


def _months(n: int, last: str = "2025-07") -> list[str]:
    y, m = map(int, last.split("-"))
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def _apply(results: list[dict], positions: dict) -> dict:
    """What itd_store.apply does, in memory — for the pure scenario tests."""
    pos = copy.deepcopy(positions)
    for r in results:
        k = (r["stream"], r["row_key"])
        if r["result"] in itd.SENT_AS_IS:
            pos[k] = {"position_id": len(pos) + 1, "hash": r["new_hash"],
                      "money": r["new_money"], "values": r["new_values"],
                      "status": "active", "adjust_count": 0}
        elif r["result"] in (itd.CHANGED, itd.DETAILS_CHANGED):
            pos[k].update(hash=r["new_hash"], money=r["new_money"],
                          values=r["new_values"], status="active")
            if r["adjust_no"]:
                pos[k]["adjust_count"] = r["adjust_no"]
        elif r["result"] == itd.DROPPED:
            pos[k]["status"] = "dropped"
    return pos


# ── config and values ───────────────────────────────────────────────────────

def test_config_defaults_to_off():
    assert itd.normalize_config(None) is None
    assert itd.normalize_config("not json") is None
    c = itd.normalize_config({})
    assert c["mode"] == "never" and not itd.enabled(c)
    # a sheet with no key columns at all cannot match anything → dropped
    c = itd.normalize_config({"mode": "auto", "sheets": {"A": {"money_columns": ["x"]}}})
    assert c["sheets"] == {} and not itd.enabled(c)
    assert itd.enabled(CFG)
    assert itd.normalize_config({"mode": "bogus", "sheets": {}})["mode"] == "never"


def test_an_empty_excel_cell_read_as_nan_is_blank():
    assert itd.norm_value(float("nan")) is None
    assert itd.to_number(float("nan")) is None


def test_values_have_one_spelling():
    assert itd.norm_value(98000) == itd.norm_value(98000.0) == itd.norm_value("98000")
    assert itd.norm_value(24999999.999999996) == "25000000"
    assert itd.norm_value("2025-08-01T00:00:00") == "2025-08-01"
    assert itd.norm_value("  INV 110582 ") == itd.norm_value("inv 110582")
    assert itd.norm_value("") is None and itd.norm_value(None) is None


def test_period_of_reads_common_date_shapes():
    assert itd.period_of("2025-08-31T00:00:00") == "2025-08"
    assert itd.period_of("20250831") == "2025-08"
    assert itd.period_of("08/31/2025") == "2025-08"
    assert itd.period_of("31/08/2025") == "2025-08"
    assert itd.period_of("31-Aug-2025") == "2025-08"
    assert itd.period_of(45900) == "2025-08"          # Excel serial
    assert itd.period_of("2026-09") == "2026-09"
    assert itd.label_period("2026-Q1") == "2026-03"
    assert itd.period_of("n/a") is None


def test_money_delta_is_to_the_cent_and_skips_zero():
    assert itd.money_delta({GWP: 10000}, {GWP: 12000}) == {GWP: 2000.0}
    assert itd.money_delta({GWP: 12000}, {GWP: 11500}) == {GWP: -500.0}
    assert itd.money_delta({GWP: 16337.750000000002}, {GWP: "16,337.75"}) == {}


# ── detection ───────────────────────────────────────────────────────────────

def test_detects_itd_from_many_reported_periods():
    rows = [_row(i, p) for i, p in enumerate(_months(12))]
    d = itd.detect(_landing(rows), CFG, "2025-07")
    assert d["is_itd"] and d["as_at"] == "2025-07" and len(d["periods"]) == 12
    # both columns read: 12 reported periods, 12 transaction months
    assert d["basis"] == "reported period and transaction date"
    assert len(d["transaction_periods"]) == 12
    assert "repeats earlier bordereaux" in d["reason"]


def test_monthly_file_with_a_backdated_row_is_not_itd():
    # A transaction effective in March, reported in July's bordereau: the
    # transaction dates cover two months, but the reported period is July
    # on every row, so the file is still a monthly one — and the March row is
    # a late transaction, loaded, not history.
    rows = [_row(i, "2025-07") for i in range(5)]
    rows[0][EFF] = "2025-03-15T00:00:00"
    d = itd.detect(_landing(rows), CFG, "2025-07")
    assert not d["is_itd"]
    assert d["transaction_periods"] == ["2025-03", "2025-07"]
    assert d["periods"] == ["2025-07"] and d["late_rows"] == 1
    assert "1 row is a late transaction reported this month" in d["reason"]


def test_many_reported_periods_make_itd_whatever_the_transaction_dates():
    # Rows reported in three bordereaux, all with an effective date in July:
    # the file repeats earlier bordereaux, so it is ITD.
    rows = [_row(i, p) for i, p in enumerate(_months(3))]
    for r in rows:
        r[EFF] = "2025-07-01T00:00:00"
    d = itd.detect(_landing(rows), CFG, "2025-07")
    assert d["is_itd"] and d["transaction_periods"] == ["2025-07"]


def test_without_a_reported_period_the_file_is_regular():
    # The client's rule (21 Aug 2026): transaction dates alone never make a
    # file ITD — a late transaction would look like history and be skipped.
    cfg = itd.normalize_config({**CFG, "sheets": {SHEET: {**CFG["sheets"][SHEET],
                                                         "period_column": "Nope"}}})
    rows = [_row(i, p) for i, p in enumerate(_months(3))]   # 3 transaction months
    d = itd.detect(_landing(rows), cfg, None)
    assert not d["is_itd"] and d["basis"] is None
    assert len(d["transaction_periods"]) == 3
    assert "Transaction dates alone cannot tell" in d["reason"]
    # the column is there but blank on every row: the same
    for r in rows:
        r[PEND] = None
    assert not itd.detect(_landing(rows), CFG, None)["is_itd"]


def test_mode_always_and_mode_never():
    rows = [_row(1, "2025-07")]
    assert itd.detect(_landing(rows), {**CFG, "mode": "always"})["is_itd"]
    assert not itd.detect(_landing(rows * 2), {**CFG, "mode": "never"})["is_itd"]


# ── the requirement's July → August example ─────────────────────────────────

def _july_august():
    months = _months(10, "2025-07")
    july = [_row(i, months[i % 10], 1000 + i) for i in range(70)]
    august = []
    for i, r in enumerate(july):
        r2 = dict(r)
        if 50 <= i < 70:                       # 20 rows whose money moved
            r2[GWP] = r[GWP] + 2000
            r2[COMM] = round(r2[GWP] * 0.15, 2)
        august.append(r2)
    august += [_row(1000 + i, "2025-08", 500 + i) for i in range(19)]   # 19 new
    return july, august


def test_first_itd_is_all_new_and_loads_everything():
    july, _ = _july_august()
    cmp = itd.compare(_landing(july), CFG, {}, load_type=itd.ITD)
    assert cmp["summary"][itd.NEW] == 70
    delta = itd.build_delta(_landing(july), CFG, cmp, load_type=itd.ITD)
    assert delta["row_count"] == 70


def test_second_itd_loads_only_39_of_89_rows():
    july, august = _july_august()
    pos = _apply(itd.compare(_landing(july), CFG, {}, load_type=itd.ITD)["results"], {})
    cmp = itd.compare(_landing(august), CFG, pos, load_type=itd.ITD)
    s = cmp["summary"]
    assert (s[itd.UNCHANGED], s[itd.CHANGED], s[itd.NEW]) == (50, 20, 19)
    assert s["rows_in_file"] == 89 and s["rows_to_load"] == 39
    delta = itd.build_delta(_landing(august), CFG, cmp, load_type=itd.ITD, as_at="2025-08")
    rows = delta["sheets"][SHEET]["rows"]
    assert delta["row_count"] == 39 and len(rows) == 39
    adj = [r for r in rows if r[itd.ADJUSTS_COL]]
    assert len(adj) == 20
    # an adjustment carries only the difference, and says what it is
    a = next(r for r in adj if r[KEY].startswith("INV100050"))
    assert a[GWP] == 2000.0 and a[COMM] == 300.0
    assert a[KEY] == "INV100050-ADJ1" and a[itd.SOURCE_REF_COL] == "INV100050-ADJ1"
    assert a[itd.ROW_KEY_COL] == "txn:inv100050#ADJ1"
    assert a[itd.ADJUSTS_COL] == "txn:inv100050"
    assert s["adjustment_totals"][GWP] == 40000.0
    assert delta["itd"]["load_type"] == itd.ITD and delta["itd"]["as_at"] == "2025-08"


def test_same_itd_again_loads_nothing():
    july, _ = _july_august()
    pos = _apply(itd.compare(_landing(july), CFG, {}, load_type=itd.ITD)["results"], {})
    cmp = itd.compare(_landing(copy.deepcopy(july)), CFG, pos, load_type=itd.ITD)
    assert cmp["summary"][itd.UNCHANGED] == 70 and cmp["summary"]["rows_to_load"] == 0
    assert itd.build_delta(_landing(july), CFG, cmp, load_type=itd.ITD)["row_count"] == 0


def test_a_row_changing_over_several_months_adds_up():
    jul = [_row(1, "2025-07", 10000)]
    pos = _apply(itd.compare(_landing(jul), CFG, {}, load_type=itd.ITD)["results"], {})
    aug = [_row(1, "2025-07", 12000), _row(2, "2025-08", 1)]
    c2 = itd.compare(_landing(aug), CFG, pos, load_type=itd.ITD)
    r = next(x for x in c2["results"] if x["row_key"] == "txn:inv100001")
    assert r["result"] == itd.CHANGED and r["delta"][GWP] == 2000.0 and r["adjust_no"] == 1
    pos = _apply(c2["results"], pos)
    sep = [_row(1, "2025-07", 11500), _row(2, "2025-08", 1), _row(3, "2025-09", 1)]
    c3 = itd.compare(_landing(sep), CFG, pos, load_type=itd.ITD)
    r = next(x for x in c3["results"] if x["row_key"] == "txn:inv100001")
    assert r["result"] == itd.CHANGED and r["delta"][GWP] == -500.0 and r["adjust_no"] == 2
    d = itd.build_delta(_landing(sep), CFG, c3, load_type=itd.ITD)
    keys = [x[KEY] for x in d["sheets"][SHEET]["rows"]]
    assert keys == ["INV100001-ADJ2", "INV100003"]
    assert 10000 + 2000 - 500 == 11500


def test_details_only_change_is_listed_not_loaded():
    jul = [_row(1, "2025-07", 10000, insured="ACME")]
    pos = _apply(itd.compare(_landing(jul), CFG, {}, load_type=itd.ITD)["results"], {})
    aug = [_row(1, "2025-07", 10000, insured="ACME Holdings"), _row(2, "2025-08")]
    c = itd.compare(_landing(aug), CFG, pos, load_type=itd.ITD)
    r = c["results"][0]
    assert r["result"] == itd.DETAILS_CHANGED and r["delta"] is None
    assert r["changed_fields"] == [INSURED]
    assert c["summary"]["rows_to_load"] == 1


def test_on_changed_flag_sends_no_adjustment():
    cfg = {**CFG, "on_changed": "flag"}
    jul = [_row(1, "2025-07", 10000)]
    pos = _apply(itd.compare(_landing(jul), cfg, {}, load_type=itd.ITD)["results"], {})
    aug = [_row(1, "2025-07", 12000), _row(2, "2025-08")]
    c = itd.compare(_landing(aug), cfg, pos, load_type=itd.ITD)
    assert c["summary"][itd.CHANGED] == 1 and c["summary"]["rows_to_load"] == 1
    d = itd.build_delta(_landing(aug), cfg, c, load_type=itd.ITD)
    assert [r[KEY] for r in d["sheets"][SHEET]["rows"]] == ["INV100002"]


def test_dropped_rows_are_listed_for_itd_only():
    jul = [_row(i, "2025-07") for i in range(3)]
    pos = _apply(itd.compare(_landing(jul), CFG, {}, load_type=itd.ITD)["results"], {})
    aug = [_row(0, "2025-07"), _row(1, "2025-07")]
    c = itd.compare(_landing(aug), CFG, pos, load_type=itd.ITD)
    assert c["summary"][itd.DROPPED] == 1
    assert c["summary"]["rows_in_file"] == 2      # a dropped row is not in the file
    reg = itd.compare(_landing(aug), CFG, pos, load_type=itd.REGULAR)
    assert reg["summary"][itd.DROPPED] == 0


def test_duplicates_and_missing_keys_are_held_back():
    rows = [_row(1), _row(1), _row(2, key=False)]
    rows[2][POL] = None                     # no fallback either
    c = itd.compare(_landing(rows), CFG, {}, load_type=itd.ITD)
    s = c["summary"]
    assert (s[itd.NEW], s[itd.DUPLICATE], s[itd.MISSING_KEY]) == (1, 1, 1)
    d = itd.build_delta(_landing(rows), CFG, c, load_type=itd.ITD)
    assert d["row_count"] == 1


def test_fallback_key_keeps_identical_looking_real_rows_apart():
    # Two genuine transactions, same policy/type/date/period/amount, no id.
    a, b = _row(1, key=False), _row(1, key=False)
    c = itd.compare(_landing([a, b]), CFG, {}, load_type=itd.ITD)
    assert c["summary"][itd.NEW] == 2
    assert {r["row_key"].rsplit("#", 1)[1] for r in c["results"]} == {"1", "2"}
    assert all(r["key_source"] == "composite" for r in c["results"])


# ── no transaction id: policy, type, date, period and amount ────────────────

def _no_id(gwp: float, i: int = 1, period: str = "2025-07") -> dict:
    """A row with no invoice number. Same i and period → same policy, type,
    date and period: look-alike rows told apart only by their amounts."""
    return _row(i, period, gwp, key=False)


def test_no_id_rows_match_on_their_amounts_whatever_the_order():
    july = [_no_id(1434), _no_id(63), _no_id(127)]
    pos = _apply(itd.compare(_landing(july), CFG, {}, load_type=itd.ITD)["results"], {})
    again = itd.compare(_landing([july[1], july[2], july[0]]), CFG, pos, load_type=itd.ITD)
    s = again["summary"]
    assert (s[itd.UNCHANGED], s[itd.CHANGED], s["rows_to_load"]) == (3, 0, 0)


def test_no_id_amount_changed_is_a_change_not_a_new_row():
    july = [_no_id(1434), _no_id(63), _no_id(127)]
    first = itd.compare(_landing(july), CFG, {}, load_type=itd.ITD)
    pos = _apply(first["results"], {})
    # 1,434 became 80, and the rows come in another order
    c = itd.compare(_landing([_no_id(63), _no_id(127), _no_id(80)]), CFG, pos,
                    load_type=itd.ITD)
    s = c["summary"]
    assert (s[itd.UNCHANGED], s[itd.CHANGED], s[itd.NEW], s[itd.DROPPED]) == (2, 1, 0, 0)
    ch = next(r for r in c["results"] if r["result"] == itd.CHANGED)
    assert ch["delta"][GWP] == -1354.0 and ch["adjust_no"] == 1
    was = next(r["row_key"] for r in first["results"] if r["new_money"][GWP] == 1434.0)
    assert ch["row_key"] == was
    d = itd.build_delta(_landing([_no_id(63), _no_id(127), _no_id(80)]), CFG, c,
                        load_type=itd.ITD)
    assert d["row_count"] == 1 and d["sheets"][SHEET]["rows"][0][GWP] == -1354.0


def test_no_id_identical_rows_are_kept_and_noted_for_a_person():
    a = _no_id(592.93)
    c = itd.compare(_landing([a, dict(a)]), CFG, {}, load_type=itd.ITD)
    assert c["summary"][itd.NEW] == 2 and c["summary"]["identical_rows"] == 2
    assert "please confirm both are real" in itd_store._note(c["results"][0])
    # delivered once before, twice now: one is that one, the other is new
    pos = _apply(itd.compare(_landing([a]), CFG, {}, load_type=itd.ITD)["results"], {})
    c = itd.compare(_landing([a, dict(a)]), CFG, pos, load_type=itd.ITD)
    s = c["summary"]
    assert (s[itd.UNCHANGED], s[itd.NEW], s["identical_rows"]) == (1, 1, 1)
    new = next(r for r in c["results"] if r["result"] == itd.NEW)
    assert new["row_key"].endswith("#2")             # never a number in use
    # both delivered: nothing left to confirm
    pos = _apply(c["results"], pos)
    c = itd.compare(_landing([a, dict(a)]), CFG, pos, load_type=itd.ITD)
    assert c["summary"][itd.UNCHANGED] == 2 and c["summary"]["identical_rows"] == 0


def test_no_id_rows_with_different_amounts_are_not_noted():
    c = itd.compare(_landing([_no_id(1434), _no_id(63)]), CFG, {}, load_type=itd.ITD)
    assert c["summary"][itd.NEW] == 2 and c["summary"]["identical_rows"] == 0
    assert itd_store._note(c["results"][0]) is None


def test_no_id_same_rows_on_two_tabs_count_once():
    cfg = itd.normalize_config({"mode": "auto", "sheets": {
        n: {"stream": "H", "key_columns": [], "fallback_key_columns": [POL, TYPE, EFF, PSTART],
            "period_column": PEND, "money_columns": [GWP, COMM]} for n in ("Monthly", SHEET)}})
    month = [_no_id(100, i, "2025-08") for i in range(3)]
    history = [_no_id(50, 10 + i) for i in range(4)] + [dict(r) for r in month]
    book = {"sheets": {"Monthly": {"rows": month}, SHEET: {"rows": history}}}
    s = itd.compare(book, cfg, {}, load_type=itd.ITD)["summary"]
    assert (s[itd.NEW], s[itd.DUPLICATE], s["duplicate_other_sheet"]) == (7, 3, 3)


def test_a_regular_file_passes_through_untouched():
    rows = [_row(i, "2025-08") for i in range(4)]
    c = itd.compare(_landing(rows), CFG, {}, load_type=itd.REGULAR)
    d = itd.build_delta(_landing(rows), CFG, c, load_type=itd.REGULAR)
    assert d["sheets"] == _landing(rows)["sheets"]
    assert d["itd"]["load_type"] == itd.REGULAR


def test_sheets_not_in_the_config_pass_through():
    land = _landing([_row(1)])
    land["sheets"]["Fac Cert"] = {"columns": ["x"], "rows": [{"x": 1}, {"x": 2}]}
    land["row_count"] = 3
    c = itd.compare(land, CFG, {}, load_type=itd.ITD)
    d = itd.build_delta(land, CFG, c, load_type=itd.ITD)
    assert d["sheets"]["Fac Cert"]["rows"] == [{"x": 1}, {"x": 2}] and d["row_count"] == 3


def test_suggest_config_finds_key_period_and_money_but_stays_off():
    layout = [(SHEET, [KEY, POL, TYPE, EFF, PSTART, PEND, GWP, COMM, INSURED,
                       "Broker Commission%", "100% Gross Written Premium",
                       "Palms part of Limit $"])]
    s = itd.suggest_config(layout)
    sc = s["sheets"][SHEET]
    assert s["mode"] == "never"
    assert sc["key_columns"] == [KEY] and sc["period_column"] == PEND
    assert GWP in sc["money_columns"] and COMM in sc["money_columns"]
    assert "100% Gross Written Premium" in sc["money_columns"]
    assert "Broker Commission%" not in sc["money_columns"]
    assert "Palms part of Limit $" not in sc["money_columns"]
    assert TYPE not in sc["money_columns"]


def test_auto_config_turns_on_only_where_id_and_period_are_found():
    layout = [(SHEET, [KEY, POL, TYPE, EFF, PSTART, PEND, GWP, COMM, INSURED]),
              ("No period", [KEY, POL, GWP]),        # id but no reported period
              ("No id", [POL, TYPE, EFF, PEND, GWP])]  # period but no id
    c = itd.auto_config(layout)
    assert c["mode"] == "auto" and c["auto_filled"] is True and itd.enabled(c)
    assert list(c["sheets"]) == [SHEET]
    sc = c["sheets"][SHEET]
    assert sc["key_columns"] == [KEY] and sc["period_column"] == PEND
    assert GWP in sc["money_columns"] and COMM in sc["money_columns"]
    # nothing usable → None, i.e. ITD stays off exactly as today
    assert itd.auto_config(layout[1:]) is None
    assert itd.auto_config([]) is None
    # anything not marked automatic reads as a person's settings
    assert itd.normalize_config({"mode": "auto", "sheets": {}})["auto_filled"] is False


def test_auto_config_turns_on_without_an_id_when_the_business_key_is_whole():
    # A broker who sends no transaction number: policy, type, transaction date
    # and reporting date are enough to match on.
    cols = [POL, TYPE, "Transaction Date", PEND, GWP]
    c = itd.auto_config([(SHEET, cols)])
    sc = c["sheets"][SHEET]
    assert sc["key_columns"] == [] and sc["period_column"] == PEND
    assert sc["fallback_key_columns"] == [POL, TYPE, "Transaction Date", PEND]
    # three of the four are not enough
    assert itd.auto_config([(SHEET, [POL, TYPE, PEND, GWP])]) is None
    # a monthly tab and an ITD tab with no id share one running total
    two = itd.auto_config([("Monthly", cols), (SHEET, cols)])
    assert two["sheets"]["Monthly"]["stream"] == two["sheets"][SHEET]["stream"]


def _reversal_of(row: dict, period: str = "2025-08") -> dict:
    return {**row, KEY: row[KEY] + "-R", GWP: -row[GWP], COMM: -row[COMM],
            PEND: f"{period}-28T00:00:00"}


def test_reversal_is_classed_and_linked_to_what_it_reverses():
    july = [_row(i, "2025-07") for i in range(3)]
    pos = _apply(itd.compare(_landing(july), CFG, {}, load_type=itd.ITD)["results"], {})
    rev = _reversal_of(july[0])
    orphan = {**_row(9, "2025-08"), KEY: "INV999999-R"}
    aug = july + [rev, orphan]
    c = itd.compare(_landing(aug), CFG, pos, load_type=itd.ITD)
    by = {r["source_ref"]: r for r in c["results"]}
    assert by["INV100000-R"]["result"] == itd.REVERSAL
    assert by["INV100000-R"]["reverses_key"] == "txn:inv100000"
    assert by["INV100000-R"]["reverses_ref"] == "INV100000"
    # its original was never delivered: still a reversal, but nothing to link
    assert by["INV999999-R"]["result"] == itd.REVERSAL
    assert by["INV999999-R"]["reverses_key"] is None
    s = c["summary"]
    assert (s[itd.REVERSAL], s[itd.UNCHANGED], s["rows_to_load"]) == (2, 3, 2)
    sent = itd.build_delta(_landing(aug), CFG, c, load_type=itd.ITD)["sheets"][SHEET]["rows"]
    assert [x[KEY] for x in sent] == ["INV100000-R", "INV999999-R"]
    assert sent[0][itd.REVERSES_COL] == "txn:inv100000" and sent[0][GWP] == -1000.0
    # original and reversal arriving together: linked inside the file
    first = itd.compare(_landing(aug), CFG, {}, load_type=itd.ITD)["results"]
    assert {r["source_ref"]: r for r in first}["INV100000-R"]["reverses_key"] == "txn:inv100000"
    # once delivered, a reversal is an ordinary transaction
    again = itd.compare(_landing(aug), CFG, _apply(c["results"], pos), load_type=itd.ITD)
    assert again["summary"][itd.UNCHANGED] == 5
    # no suffixes, no reversals
    off = itd.normalize_config({**CFG, "reversal_suffixes": []})
    assert {r["result"] for r in itd.compare(_landing([rev]), off, {},
                                             load_type=itd.ITD)["results"]} == {itd.NEW}


def test_tabs_with_the_same_transactions_share_one_running_total():
    # A monthly tab and an ITD tab of one workbook: same id column, the same
    # headings but one, named differently on each tab (as in Risksmith).
    base = [KEY, POL, TYPE, EFF, PSTART, PEND, GWP, COMM, INSURED]
    layout = [("Monthly", base + ["Net Payable"]), (SHEET, base + ["Net Payable $"]),
              ("Claims", [KEY, POL, PEND, "Paid Amount", "Reserve"])]
    cfg = itd.auto_config(layout)
    m, i = cfg["sheets"]["Monthly"], cfg["sheets"][SHEET]
    assert m["stream"] == i["stream"] == f"Monthly + {SHEET}"
    assert cfg["sheets"]["Claims"]["stream"] == "Claims"          # too different
    assert "Net Payable" in m["ignore_columns"] and "Net Payable $" in i["ignore_columns"]
    assert "Net Payable" not in m["money_columns"] and GWP in m["money_columns"]
    assert PEND in m["fallback_key_columns"] and PSTART not in m["fallback_key_columns"]
    july = [_row(n, p) for n, p in enumerate(_months(6, "2025-07"))]
    aug = [_row(100 + n, "2025-08") for n in range(3)]
    monthly = [{**r, "Net Payable": 1} for r in aug]
    itd_tab = [{**r, "Net Payable $": 1} for r in july + aug]
    book = {"sheets": {"Monthly": {"rows": monthly}, SHEET: {"rows": itd_tab}}}
    c = itd.compare(book, cfg, {}, load_type=itd.ITD)
    s = c["summary"]
    assert (s["rows_to_load"], s[itd.DUPLICATE], s["duplicate_other_sheet"]) == (9, 3, 3)
    dup = next(r for r in c["results"] if r["result"] == itd.DUPLICATE)
    assert dup["sheet"] == SHEET and dup["duplicate_of_sheet"] == "Monthly"
    # next month, the ITD tab alone: the 3 August rows came from the monthly
    # tab, and still match — the differently named column is not compared
    again = itd.compare({"sheets": {SHEET: {"rows": itd_tab}}}, cfg,
                        _apply(c["results"], {}), load_type=itd.ITD)
    assert again["summary"][itd.UNCHANGED] == 9 and again["summary"]["rows_to_load"] == 0


# ── the running total, on SQLite ────────────────────────────────────────────

@pytest.fixture()
def session():
    from canonical import CANONICAL_TABLES, canonical_metadata
    eng = create_engine("sqlite://")
    canonical_metadata.create_all(eng, tables=[CANONICAL_TABLES["bdx_sent_transaction"],
                                               CANONICAL_TABLES["bdx_row_comparison"]])
    with eng.begin() as c:
        c.execute(text("CREATE TABLE pipeline (id INTEGER PRIMARY KEY, itd_config JSON)"))
        c.execute(text("INSERT INTO pipeline (id) VALUES (7)"))
    S = sessionmaker(bind=eng, future=True)
    with S() as s:
        yield s


FEED = dict(tenant_id=1, program_id=10, broker_id=20)


def _run(s, landing, cfg, *, arrival_id, landing_id, load_type=None, apply=True,
         check_only=False):
    """A run as itd_run does it: detect, compare, stage, then apply."""
    det = itd.detect(landing, cfg)
    lt = load_type or (itd.ITD if det["is_itd"] else itd.REGULAR)
    pos = itd_store.load_positions(s, FEED["tenant_id"], FEED["program_id"],
                                   FEED["broker_id"], [SHEET])
    cmp = itd.compare(landing, cfg, pos, load_type=lt)
    itd_store.stage(s, **FEED, arrival_id=arrival_id, landing_id=landing_id,
                    load_type=lt, period=det["as_at"], results=cmp["results"],
                    check_only=check_only)
    s.commit()
    res = None
    if apply:
        res = itd_store.apply(s, landing_id, arrival_id=arrival_id)
        s.commit()
    return cmp, res


def test_old_table_names_are_moved_across_with_their_rows():
    # A database from before the rename: bordereau_position(_event), columns
    # position_* / event_*. The rows must survive under the new names.
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE bordereau_position (position_id INTEGER PRIMARY KEY, "
                       "tenant_id INTEGER, position_stream TEXT, position_status TEXT)"))
        c.execute(text("CREATE TABLE bordereau_position_event (event_id INTEGER PRIMARY KEY, "
                       "event_position_id INTEGER, event_result TEXT, created_at TEXT)"))
        c.execute(text("INSERT INTO bordereau_position VALUES (1, 7, 'ITD (H)', 'active')"))
        c.execute(text("INSERT INTO bordereau_position_event VALUES (5, 1, 'new', 'x')"))
    with eng.begin() as c:
        done = itd_store.rename_old_tables(c)
    assert "bordereau_position -> bdx_sent_transaction" in done
    with eng.begin() as c:
        assert c.execute(text("SELECT sent_id, tenant_id, sent_stream, sent_status "
                              "FROM bdx_sent_transaction")).all() == [(1, 7, "ITD (H)", "active")]
        assert c.execute(text("SELECT comparison_id, comparison_sent_id, comparison_result, "
                              "created_at FROM bdx_row_comparison")).all() == [(5, 1, "new", "x")]
        assert itd_store.rename_old_tables(c) == []          # nothing left to do


def test_store_config_round_trip(session):
    assert itd_store.config_column_present(session) and itd_store.tables_present(session)
    assert itd_store.read_config(session, 7) is None
    itd_store.write_config(session, 7, {"mode": "auto", "sheets": {SHEET: {"key_columns": [KEY]}}})
    session.commit()
    c = itd_store.read_config(session, 7)
    assert c["mode"] == "auto" and c["sheets"][SHEET]["key_columns"] == [KEY]
    itd_store.write_config(session, 7, {})
    session.commit()
    assert itd_store.read_config(session, 7) is None


def test_store_auto_fill_never_overwrites_a_persons_settings(session):
    from types import SimpleNamespace
    session.execute(text("INSERT INTO pipeline (id) VALUES (8)"))
    pipe, other = SimpleNamespace(id=7, input_format_id=None), SimpleNamespace(id=8)
    layout = [(SHEET, [KEY, POL, TYPE, EFF, PSTART, PEND, GWP, COMM])]
    # a new setup: switched on from its own sheets
    c = itd_store.auto_fill(session, pipe, layout=layout)
    session.commit()
    assert c["mode"] == "auto" and c["auto_filled"]
    assert itd_store.read_config(session, 7) == c
    # created again (or by a second call): the stored settings are kept
    assert itd_store.auto_fill(session, pipe, layout=[]) == c
    # input template swapped before anybody saved: redone from the new sheets
    renamed = [(SHEET, [KEY, POL, TYPE, EFF, PSTART, PEND, "Net Premium"])]
    c2 = itd_store.auto_fill(session, pipe, refill=True, layout=renamed)
    session.commit()
    assert c2["sheets"][SHEET]["money_columns"] == ["Net Premium"]
    # ... and to sheets with no id + period: back to off
    assert itd_store.auto_fill(session, pipe, refill=True, layout=[("X", [POL])]) is None
    session.commit()
    assert itd_store.read_config(session, 7) is None
    # a person's saved settings are never touched
    itd_store.write_config(session, 7, {**c, "auto_filled": False, "mode": "always"})
    session.commit()
    kept = itd_store.auto_fill(session, pipe, refill=True, layout=renamed)
    assert kept["mode"] == "always" and not kept["auto_filled"]
    # a setup with no settings is not switched on by a template swap
    assert itd_store.auto_fill(session, other, refill=True, layout=layout) is None
    assert itd_store.read_config(session, 8) is None


def test_store_july_then_august(session):
    july, august = _july_august()
    _, res = _run(session, _landing(july), CFG, arrival_id=1, landing_id=101)
    assert res == {"applied": 70, "conflicts": 0}
    assert itd_store.last_itd_period(session, 1, 10, 20) == "2025-07"
    cmp, res = _run(session, _landing(august), CFG, arrival_id=2, landing_id=102)
    assert cmp["summary"]["rows_to_load"] == 39
    assert res["conflicts"] == 0
    assert itd_store.last_itd_period(session, 1, 10, 20) == "2025-08"
    pos = itd_store.load_positions(session, 1, 10, 20, [SHEET])
    assert len(pos) == 89
    p = pos[(SHEET, "txn:inv100050")]
    assert p["money"][GWP] == 3050.0 and p["adjust_count"] == 1
    rows = itd_store.file_rows(session, 1, arrival_id=2, result=itd.CHANGED)
    assert rows["total"] == 20 and rows["counts"][itd.UNCHANGED] == 50
    assert rows["rows"][0]["money_delta"] == {GWP: 2000.0, COMM: 300.0}


def test_store_each_row_says_which_month_it_is_reported_in(session):
    july, august = _july_august()
    _run(session, _landing(july), CFG, arrival_id=1, landing_id=101)
    # August repeats July's 70 rows (reported over 10 earlier months), drops one,
    # and adds 19 of its own month
    gone = august.pop(0)
    _run(session, _landing(august), CFG, arrival_id=2, landing_id=102)
    out = itd_store.file_rows(session, 1, arrival_id=2, limit=1000)
    assert out["month_counts"] == {"current": 19, "earlier": 69, "unknown": 0}
    by_ref = {r["source_ref"]: r for r in out["rows"]}
    assert by_ref["INV101000"]["row_period"] == "2025-08"
    assert by_ref["INV100001"]["row_period"] == itd.period_of(july[1][PEND])
    assert by_ref["INV100001"]["period"] == "2025-08"          # the file's month
    # a row missing from the file: the month it was reported in before
    dropped = by_ref[gone[KEY]]
    assert dropped["result"] == itd.DROPPED and dropped["row_period"] == itd.period_of(gone[PEND])


def test_store_no_id_rows_reordered_changed_and_doubled(session):
    cfg = itd.normalize_config({**CFG, "sheets": {SHEET: {**CFG["sheets"][SHEET],
                                                          "key_columns": []}}})
    july = [_no_id(1434), _no_id(63), _no_id(127), _no_id(500, 2)]
    _run(session, _landing(july), cfg, arrival_id=1, landing_id=101, load_type=itd.ITD)
    # re-ordered, 1,434 became 80, and the 500 row comes twice
    aug = [_no_id(127), _no_id(80), _no_id(63), _no_id(500, 2), _no_id(500, 2)]
    cmp, res = _run(session, _landing(aug), cfg, arrival_id=2, landing_id=102,
                    load_type=itd.ITD)
    s = cmp["summary"]
    assert (s[itd.UNCHANGED], s[itd.CHANGED], s[itd.NEW], s["identical_rows"]) == (3, 1, 1, 1)
    assert res["conflicts"] == 0
    assert len(itd_store.load_positions(session, 1, 10, 20, [SHEET])) == 5
    new = itd_store.file_rows(session, 1, arrival_id=2, result=itd.NEW)["rows"]
    assert "please confirm both are real" in new[0]["note"]


def test_store_apply_is_idempotent_and_a_failed_run_changes_nothing(session):
    july, _ = _july_august()
    # A run that never got to apply (it failed): the running total is untouched…
    _run(session, _landing(july), CFG, arrival_id=1, landing_id=101, apply=False)
    assert itd_store.load_positions(session, 1, 10, 20, [SHEET]) == {}
    # …so its rerun compares against the same state and still sees 70 new.
    cmp, res = _run(session, _landing(july), CFG, arrival_id=1, landing_id=102)
    assert cmp["summary"][itd.NEW] == 70 and res["applied"] == 70
    again = itd_store.apply(session, 102)
    assert again == {"applied": 0, "conflicts": 0}


def test_store_check_only_never_moves_the_total(session):
    july, _ = _july_august()
    _run(session, _landing(july), CFG, arrival_id=None, landing_id=100, check_only=True,
         apply=False)
    assert itd_store.apply(session, 100) == {"applied": 0, "conflicts": 0}
    assert itd_store.load_positions(session, 1, 10, 20, [SHEET]) == {}


def test_store_two_files_at_once_cannot_both_add_a_row(session):
    july, _ = _july_august()
    _run(session, _landing(july[:5]), CFG, arrival_id=1, landing_id=101, apply=False)
    _run(session, _landing(july[:5]), CFG, arrival_id=2, landing_id=102, apply=False)
    assert itd_store.apply(session, 101) == {"applied": 5, "conflicts": 0}
    session.commit()
    # Same rows, same values: the second file's "new" rows are already there
    # with the same fingerprint — applied, nothing doubled.
    assert itd_store.apply(session, 102)["conflicts"] == 0
    assert len(itd_store.load_positions(session, 1, 10, 20, [SHEET])) == 5


def test_store_conflict_when_another_file_moved_the_row(session):
    jul = [_row(1, "2025-07", 10000), _row(2, "2025-06", 1)]
    _run(session, _landing(jul), CFG, arrival_id=1, landing_id=101)
    a = [_row(1, "2025-07", 12000), _row(2, "2025-06", 1)]
    b = [_row(1, "2025-07", 13000), _row(2, "2025-06", 1)]
    _run(session, _landing(a), CFG, arrival_id=2, landing_id=102, apply=False)
    _run(session, _landing(b), CFG, arrival_id=3, landing_id=103, apply=False)
    assert itd_store.apply(session, 102)["conflicts"] == 0
    session.commit()
    assert itd_store.apply(session, 103)["conflicts"] == 1


def test_store_withdraw_restores_the_previous_total(session):
    jul = [_row(1, "2025-07", 10000), _row(2, "2025-06", 1)]
    _run(session, _landing(jul), CFG, arrival_id=1, landing_id=101)
    aug = [_row(1, "2025-07", 12000), _row(3, "2025-08", 5)]
    _run(session, _landing(aug), CFG, arrival_id=2, landing_id=102)
    pos = itd_store.load_positions(session, 1, 10, 20, [SHEET])
    assert pos[(SHEET, "txn:inv100002")]["status"] == "dropped"
    # July cannot be withdrawn while August is built on it.
    with pytest.raises(itd_store.CannotWithdraw):
        itd_store.withdraw(session, 1)
    assert itd_store.withdraw(session, 2) == 3
    session.commit()
    pos = itd_store.load_positions(session, 1, 10, 20, [SHEET])
    assert pos[(SHEET, "txn:inv100001")]["money"][GWP] == 10000.0
    assert pos[(SHEET, "txn:inv100002")]["status"] == "active"
    assert (SHEET, "txn:inv100003") not in pos
    assert itd_store.last_itd_period(session, 1, 10, 20) == "2025-07"


def test_store_dropped_decision(session):
    _run(session, _landing([_row(1, "2025-07"), _row(2, "2025-06")]), CFG,
         arrival_id=1, landing_id=101)
    _run(session, _landing([_row(1, "2025-07"), _row(3, "2025-08")]), CFG,
         arrival_id=2, landing_id=102)
    pid = itd_store.load_positions(session, 1, 10, 20, [SHEET])[(SHEET, "txn:inv100002")]["position_id"]
    with pytest.raises(ValueError):
        itd_store.decide_dropped(session, 1, pid, "maybe")
    assert itd_store.decide_dropped(session, 1, pid, "removed")["status"] == "removed"
    session.commit()
    # removed → a later ITD that carries it again treats it as new
    pos = itd_store.load_positions(session, 1, 10, 20, [SHEET])
    c = itd.compare(_landing([_row(2, "2025-06")]), CFG, pos, load_type=itd.ITD)
    assert c["results"][0]["result"] == itd.NEW


def test_store_regular_files_feed_the_running_total(session):
    # Monthly files first, then the first ITD: rows the monthly files already
    # delivered are not delivered again.
    for i, m in enumerate(_months(3, "2025-07")):
        _run(session, _landing([_row(i, m)]), CFG, arrival_id=10 + i,
             landing_id=200 + i, load_type=itd.REGULAR)
    itd_rows = [_row(i, m) for i, m in enumerate(_months(3, "2025-07"))] + [_row(9, "2025-08")]
    cmp, _ = _run(session, _landing(itd_rows), CFG, arrival_id=20, landing_id=300)
    assert cmp["summary"][itd.UNCHANGED] == 3 and cmp["summary"][itd.NEW] == 1


def test_store_reversal_keeps_its_link_and_can_be_withdrawn(session):
    july = [_row(i) for i in range(2)]
    _run(session, _landing(july), CFG, arrival_id=1, landing_id=1, load_type=itd.ITD)
    rev = _reversal_of(july[0])
    _, res = _run(session, _landing(july + [rev]), CFG, arrival_id=2, landing_id=2,
                  load_type=itd.ITD)
    assert res == {"applied": 3, "conflicts": 0}
    p = itd_store._pos()
    pos = session.execute(select(p).where(p.c.sent_source_ref == "INV100000-R")
                          ).mappings().one()
    assert pos["sent_reverses_key"] == "txn:inv100000"
    row = itd_store.file_rows(session, 1, arrival_id=2, result=itd.REVERSAL)["rows"][0]
    assert row["reverses_ref"] == "INV100000" and row["note"] == "Reversal of INV100000."
    assert itd_store.withdraw(session, 2) == 3
    session.commit()
    assert session.execute(select(p.c.sent_status).where(
        p.c.sent_source_ref == "INV100000-R")).scalar() == "void"


def test_rows_are_reported_in_the_clients_classes(session):
    # The register: New / Previously processed / Changed / Cancelled or reversed.
    july = [_row(i) for i in range(5)]
    _run(session, _landing(july), CFG, arrival_id=1, landing_id=1, load_type=itd.ITD)
    aug = [dict(r) for r in july[1:]]                       # INV100000 dropped
    aug[0][GWP] = 1500.0                                     # amount changed
    aug[1][INSURED] = "Other"                                # details only
    aug += [_reversal_of(july[2]), _row(50, "2025-08"),      # reversal, new
            dict(_row(50, "2025-08")), _row(60, "2025-08", key=False) | {POL: None}]
    cmp, _ = _run(session, _landing(aug), CFG, arrival_id=2, landing_id=2,
                  load_type=itd.ITD)
    assert cmp["summary"]["classes"] == {
        "new": 1, "previously_processed": 2, "changed": 2,
        "cancelled_or_reversed": 2, "held_back": 2}
    rows = itd_store.file_rows(session, 1, arrival_id=2, cls=itd.CLASS_CANCELLED)
    assert {r["result"] for r in rows["rows"]} == {itd.REVERSAL, itd.DROPPED}
    assert all(r["class"] == itd.CLASS_CANCELLED for r in rows["rows"])
    assert rows["class_counts"][itd.CLASS_CHANGED] == 2

def test_store_withdraw_also_voids_results_still_pending(session):
    july = [_row(i) for i in range(2)]
    _run(session, _landing(july), CFG, arrival_id=1, landing_id=1, apply=False)
    assert itd_store.has_effect(session, 1)
    assert itd_store.withdraw(session, 1) == 0
    session.commit()
    assert not itd_store.has_effect(session, 1)


# ── the real Risksmith ITD, when it is on disk ──────────────────────────────

_SAMPLE = os.environ.get("ITD_SAMPLE_XLSX") or os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "Document", "ITD Documents",
    "2025-08 - Risksmith - Schedule H_YTD_corrected_fac_reins_pol_nums.xlsx")


def _risksmith():
    if not os.path.exists(_SAMPLE):
        pytest.skip("Risksmith ITD sample not on disk")
    import pandas as pd
    import direct_lane as dl
    xl = pd.read_excel(_SAMPLE, sheet_name=["ITD (H)", "Written BDX (H)"], header=0)
    xl = {k: v.loc[:, [c for c in v.columns if not str(c).startswith("Unnamed")]]
          for k, v in xl.items()}
    landing = dl.build_landing_record(xl)
    cfg = itd.suggest_config([(n, sh["columns"]) for n, sh in landing["sheets"].items()])
    cfg["mode"] = "auto"
    cfg = itd.normalize_config(cfg)
    return landing, cfg


def test_risksmith_sample_detection():
    landing, cfg = _risksmith()
    sc = cfg["sheets"]["ITD (H)"]
    assert sc["key_columns"] == ["Risksmith Invoice Number"]
    itd_tab = {"sheets": {"ITD (H)": landing["sheets"]["ITD (H)"]}}
    month_tab = {"sheets": {"Written BDX (H)": landing["sheets"]["Written BDX (H)"]}}
    assert itd.detect(itd_tab, cfg, "2025-08")["is_itd"]
    assert not itd.detect(month_tab, cfg, "2025-08")["is_itd"]


def test_risksmith_sample_monthly_tab_needs_both_columns():
    # The monthly tab's 28 rows have transaction dates over 10 months
    # (2024-10 to 2025-08) — on those alone it would look like ITD. Every row
    # is reported in 2025-08, so it is a monthly file with 25 late
    # transactions. The ITD tab is reported over 20 months.
    landing, cfg = _risksmith()
    month = itd.detect({"sheets": {"Written BDX (H)": landing["sheets"]["Written BDX (H)"]}},
                       cfg, "2025-08")
    assert not month["is_itd"] and month["periods"] == ["2025-08"]
    assert len(month["transaction_periods"]) == 10 and month["late_rows"] == 25
    whole = itd.detect({"sheets": {"ITD (H)": landing["sheets"]["ITD (H)"]}}, cfg, "2025-08")
    assert whole["is_itd"] and len(whole["periods"]) == 20
    assert whole["as_at"] == "2025-08"


def test_risksmith_sample_auto_config():
    # What a new setup built on the Risksmith workbook starts with — no
    # Fac Cert or other tab without an invoice number and a reporting date.
    landing, _ = _risksmith()
    c = itd.auto_config([(n, sh["columns"]) for n, sh in landing["sheets"].items()])
    sc = c["sheets"]["ITD (H)"]
    assert c["mode"] == "auto" and c["auto_filled"]
    assert sc["key_columns"] == ["Risksmith Invoice Number"]
    assert sc["period_column"] == "Reporting End Date"
    assert sc["money_columns"]
    # used only when the invoice number is blank: the client's business key,
    # with the amount compared rather than keyed on
    assert sc["fallback_key_columns"] == [
        "Risksmith Policy Number", "Transaction Type - Original Premium etc",
        "Effective Date of Transaction US", "Reporting End Date"]
    rows = landing["sheets"]["ITD (H)"]["rows"]
    assert itd.detect({"sheets": {"ITD (H)": {"rows": rows}}}, c, "2025-08")["is_itd"]


def test_risksmith_sample_reversals_are_linked():
    # 30 "-R" rows on the ITD tab, every one with its original in the file.
    landing, cfg = _risksmith()
    c = itd.compare({"sheets": {"ITD (H)": landing["sheets"]["ITD (H)"]}}, cfg, {},
                    load_type=itd.ITD)
    revs = {r["source_ref"]: r for r in c["results"] if r["result"] == itd.REVERSAL}
    assert len(revs) == 30 and all(r["reverses_key"] for r in revs.values())
    assert revs["INV110582-R"]["reverses_ref"] == "INV110582"


def test_risksmith_sample_two_tabs_count_once():
    # The whole workbook: 28 August transactions on the monthly tab AND the
    # ITD tab. One running total for both tabs → 320 sent, not 348.
    landing, _ = _risksmith()
    book = {"sheets": {n: landing["sheets"][n] for n in ("Written BDX (H)", "ITD (H)")}}
    cfg = itd.auto_config([(n, sh["columns"]) for n, sh in book["sheets"].items()])
    assert cfg["sheets"]["Written BDX (H)"]["stream"] == cfg["sheets"]["ITD (H)"]["stream"]
    c = itd.compare(book, cfg, {}, load_type=itd.ITD)
    s = c["summary"]
    assert (s["rows_to_load"], s["duplicate_other_sheet"]) == (320, 28)
    pos = _apply(c["results"], {})
    itd_only = {"sheets": {"ITD (H)": book["sheets"]["ITD (H)"]}}
    assert itd.compare(itd_only, cfg, pos, load_type=itd.ITD)["summary"][itd.UNCHANGED] == 320
    # the monthly file first, on its own, then the whole workbook: 292 more
    month = {"sheets": {"Written BDX (H)": book["sheets"]["Written BDX (H)"]}}
    pos = _apply(itd.compare(month, cfg, {}, load_type=itd.REGULAR)["results"], {})
    assert itd.compare(book, cfg, pos, load_type=itd.ITD)["summary"]["rows_to_load"] == 292


def test_risksmith_sample_july_then_august():
    landing, cfg = _risksmith()
    rows = landing["sheets"]["ITD (H)"]["rows"]
    july = [r for r in rows if itd.period_of(r["Reporting End Date"]) <= "2025-07"]
    assert len(rows) == 320 and len(july) == 292
    c1 = itd.compare({"sheets": {"ITD (H)": {"rows": july}}}, cfg, {}, load_type=itd.ITD)
    s1 = c1["summary"]
    assert s1[itd.NEW] + s1[itd.REVERSAL] == 292 and s1["rows_to_load"] == 292
    assert s1[itd.DUPLICATE] == 0
    pos = _apply(c1["results"], {})
    c2 = itd.compare({"sheets": {"ITD (H)": {"rows": rows}}}, cfg, pos, load_type=itd.ITD)
    s = c2["summary"]
    assert (s[itd.UNCHANGED], s[itd.NEW], s[itd.CHANGED]) == (292, 28, 0)
    # the 28 new rows are exactly the August monthly tab's invoices
    new_refs = {r["source_ref"] for r in c2["results"] if r["result"] == itd.NEW}
    month_refs = {r["Risksmith Invoice Number"]
                  for r in landing["sheets"]["Written BDX (H)"]["rows"]}
    assert new_refs == month_refs


def test_risksmith_sample_both_tabs_in_one_stream():
    # A workbook carrying the same August rows on its monthly tab AND its ITD
    # tab: with both tabs in one stream, each transaction is counted once.
    landing, cfg = _risksmith()
    raw = {"mode": "auto", "sheets": {
        n: {**cfg["sheets"]["ITD (H)"], "stream": "Schedule H"}
        for n in ("Written BDX (H)", "ITD (H)")}}
    cfg2 = itd.normalize_config(raw)
    c = itd.compare(landing, cfg2, {}, load_type=itd.ITD)
    s = c["summary"]
    assert s[itd.NEW] + s[itd.REVERSAL] == 320 and s[itd.DUPLICATE] == 28


def test_risksmith_sample_without_invoice_numbers():
    # The client's ITD tab with its invoice column taken out: matched on
    # policy, type, date, period and amounts. July, then August in reverse
    # order — 54 rows share the first four with another row, a reversal with
    # its original among them, so matching by order alone made 37 false changes.
    landing, _ = _risksmith()
    inv = "Risksmith Invoice Number"
    cols = [c for c in landing["sheets"]["ITD (H)"]["columns"] if c != inv]
    rows = [{k: v for k, v in r.items() if k != inv} for r in landing["sheets"]["ITD (H)"]["rows"]]
    cfg = itd.auto_config([("ITD (H)", cols)])
    assert cfg["sheets"]["ITD (H)"]["key_columns"] == []

    def land(rs):
        return {"sheets": {"ITD (H)": {"columns": cols, "rows": rs}}}

    july = [r for r in rows if itd.period_of(r["Reporting End Date"]) <= "2025-07"]
    pos = _apply(itd.compare(land(july), cfg, {}, load_type=itd.ITD)["results"], {})
    s = itd.compare(land(rows[::-1]), cfg, pos, load_type=itd.ITD)["summary"]
    assert (s[itd.NEW], s[itd.UNCHANGED], s[itd.CHANGED], s[itd.DROPPED]) == (28, 292, 0, 0)


# ── the data-model load ─────────────────────────────────────────────────────

def test_data_model_spec_adds_keys_only_where_transactions_load():
    spec = {SHEET: {"policy_number": "Policy Number",
                    "premium_transaction_gross_premium_amount": GWP},
            "Fac Cert": {"policy_number": "Policy Number"}}
    out = itd.data_model_spec(spec)
    assert out[SHEET]["premium_transaction_row_key"] == itd.ROW_KEY_COL
    assert out[SHEET]["premium_transaction_itd_adjusts_row_key"] == itd.ADJUSTS_COL
    assert out[SHEET]["premium_transaction_source_ref"] == itd.SOURCE_REF_COL
    assert "premium_transaction_row_key" not in out["Fac Cert"]
    kept = itd.data_model_spec({SHEET: {**spec[SHEET],
                                        "premium_transaction_source_ref": KEY}})
    assert kept[SHEET]["premium_transaction_source_ref"] == KEY
    assert spec[SHEET].get("premium_transaction_row_key") is None   # not mutated


@pytest.fixture()
def canonical_session():
    from canonical import canonical_metadata
    eng = create_engine("sqlite://")
    canonical_metadata.create_all(eng)
    S = sessionmaker(bind=eng, future=True)
    with S() as s:
        yield s


def _txn_record(key: str, amount: float, ref: str) -> dict:
    return {"policy": {"policy_number": "POL-1", "policy_effective_date": "2025-01-01"},
            "premium_transaction": {"premium_transaction_gross_premium_amount": amount,
                                    "premium_transaction_row_key": key,
                                    "premium_transaction_source_ref": ref,
                                    "premium_transaction_effective_date": "2025-01-01"}}


def _txns(s):
    from sqlalchemy import select
    from canonical import CANONICAL_TABLES
    pt = CANONICAL_TABLES["premium_transaction"]
    return s.execute(select(pt.c.premium_transaction_row_key,
                            pt.c.premium_transaction_source_ref,
                            pt.c.is_current_version)
                     .order_by(pt.c.premium_transaction_id)).all()


def test_append_mode_keeps_earlier_transactions_and_skips_loaded_ones(canonical_session):
    import ingester
    s = canonical_session
    ingester.ingest_record(s, "T1", _txn_record("txn:a", 100, "A"), mode="append")
    ingester.ingest_record(s, "T1", _txn_record("txn:b", 50, "B"), mode="append")
    # the same delta loaded twice adds nothing
    ingester.ingest_record(s, "T1", _txn_record("txn:a", 100, "A"), mode="append")
    s.commit()
    rows = _txns(s)
    assert [(k, r) for k, r, _ in rows] == [("txn:a", "A"), ("txn:b", "B")]
    assert all(cur is not False for *_, cur in rows)


def test_default_mode_is_unchanged_it_replaces_the_policys_transactions(canonical_session):
    import ingester
    s = canonical_session
    ingester.ingest_record(s, "T1", _txn_record("txn:a", 100, "A"))
    ingester.ingest_record(s, "T1", _txn_record("txn:c", 7, "C"))
    s.commit()
    current = [k for k, _, cur in _txns(s) if cur is not False]
    assert current == ["txn:c"]


# ── a run: itd_run.prepare → stage → apply ──────────────────────────────────

class _Pipe:
    id, program_id, broker_party_id = 7, 10, 20


@pytest.fixture()
def run_session(session):
    session.execute(text("CREATE TABLE landing_record (id INTEGER PRIMARY KEY, "
                         "output_export_id INTEGER)"))
    session.commit()
    return session


def _prepare(s, landing, **kw):
    import itd_run
    return itd_run.prepare(s, tenant_id=1, pipe=_Pipe(), program_id=10,
                           broker_party_id=20, landing=landing, **kw)


def _land(s, landing, plan):
    """What _prepare_run does after prepare: write the landing, then stage."""
    import itd_run
    lid = s.execute(text("INSERT INTO landing_record (output_export_id) VALUES (NULL) "
                         "RETURNING id")).scalar()
    s.commit()
    itd_run.stage(s, plan, lid)
    s.commit()
    return lid


def test_run_with_itd_off_is_untouched(run_session):
    july, _ = _july_august()
    land = _landing(july)
    out, plan = _prepare(run_session, land)
    assert plan is None and out is land


def test_run_first_itd_then_older_itd_is_refused(run_session):
    import itd_run
    s = run_session
    itd_store.write_config(s, 7, CFG)
    s.commit()
    july, august = _july_august()
    out, plan = _prepare(s, _landing(august))           # an ITD as at 2025-08
    assert plan["load_type"] == itd.ITD and out["row_count"] == 89
    lid = _land(s, out, plan)
    itd_store.apply(s, lid)
    s.commit()
    # July's ITD arriving after August's: kept, not loaded.
    with pytest.raises(itd_run.OlderItdFile):
        _prepare(s, _landing(july))
    # …but a broker's self-check may still look at it.
    out, plan = _prepare(s, _landing(july), check_only=True)
    assert plan["check_only"] and plan["summary"][itd.UNCHANGED] == 50


def test_run_completed_but_unapplied_is_healed_by_the_next_file(run_session):
    s = run_session
    itd_store.write_config(s, 7, CFG)
    s.commit()
    july, august = _july_august()
    out, plan = _prepare(s, _landing(july))
    lid = _land(s, out, plan)
    # the run produced its output… but its finish() never ran
    s.execute(text("UPDATE landing_record SET output_export_id = 555 WHERE id = :i"),
              {"i": lid})
    s.commit()
    out, plan = _prepare(s, _landing(august))
    assert plan["summary"]["rows_to_load"] == 39   # July was applied first


# ── a corrected file: the next version of the same submission ───────────────

@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture()
def arrival_session(run_session):
    # intake_models imports db, which only builds an engine (never connects).
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    import intake_models
    intake_models.FileArrival.__table__.create(run_session.get_bind())
    return run_session


def _arrival(s, ref: str, version: int, status=None):
    from intake_models import FileArrival
    # SQLite numbers only INTEGER keys by itself; the column is BIGINT.
    next_id = (s.execute(text("SELECT MAX(arrival_id) FROM file_arrival")).scalar() or 0) + 1
    a = FileArrival(id=next_id, tenant_id=1, filename=f"{ref}-v{version}.xlsx",
                    outcome="accepted",
                    received_at=datetime.utcnow(), submission_ref=ref,
                    version_no=version, version_status=status,
                    public_ref=f"{ref}-{version}")
    s.add(a)
    s.commit()
    return a


def _run_file(s, landing, arrival):
    out, plan = _prepare(s, landing, arrival_id=arrival.id)
    lid = _land(s, out, plan)
    itd_store.apply(s, lid, arrival_id=arrival.id)
    s.commit()
    return plan


def test_run_corrected_version_is_compared_without_the_undelivered_one(arrival_session):
    s = arrival_session
    itd_store.write_config(s, 7, CFG)
    s.commit()
    july, august = _july_august()
    _run_file(s, _landing(july), _arrival(s, "SUB-JUL", 1, "delivered"))
    # August, version 1: back with exceptions, so never delivered
    v1 = _arrival(s, "SUB-AUG", 1, "with_exceptions")
    assert _run_file(s, _landing(august), v1)["summary"]["rows_to_load"] == 39
    # the broker corrects it and sends version 2: still 39 rows, not 0
    v2 = _arrival(s, "SUB-AUG", 2)
    plan = _run_file(s, _landing(august), v2)
    assert plan["summary"]["rows_to_load"] == 39
    assert plan["summary"]["replaced_versions"] == [1]
    assert "version 1 was never delivered" in plan["reason"]
    s.refresh(v1)
    assert v1.itd_summary["status"] == "withdrawn" and v1.itd_summary["replaced_by"] == 2
    assert not itd_store.has_effect(s, v1.id)
    # a delivered version is never undone: the carrier has it
    v2.version_status = "delivered"
    s.commit()
    out, plan = _prepare(s, _landing(august), arrival_id=_arrival(s, "SUB-AUG", 3).id)
    assert plan["summary"]["rows_to_load"] == 0
    assert "replaced_versions" not in plan["summary"]


def test_run_refused_older_itd_leaves_the_earlier_version_alone(arrival_session):
    import itd_run
    s = arrival_session
    cfg = itd.normalize_config({**CFG, "sheets": {**CFG["sheets"],
                                                  "Other": CFG["sheets"][SHEET]}})
    itd_store.write_config(s, 7, cfg)
    s.commit()
    july, august = _july_august()
    v1 = _arrival(s, "SUB-A", 1, "with_exceptions")
    _run_file(s, _landing(august), v1)
    # a later ITD on another tab of the feed, delivered: as at September
    sept = [_row(500 + i, p) for i, p in enumerate(_months(2, "2025-09"))]
    _run_file(s, _landing(sept, "Other"), _arrival(s, "SUB-X", 1, "delivered"))
    # version 2 of SUB-A is July's ITD — older than September: refused, and
    # version 1 must stay exactly as it was
    v2 = _arrival(s, "SUB-A", 2)
    with pytest.raises(itd_run.OlderItdFile):
        _prepare(s, _landing(july), arrival_id=v2.id)
    s.refresh(v1)
    assert itd_store.has_effect(s, v1.id)
    assert (v1.itd_summary or {}).get("status") != "withdrawn"


# ── amount columns recognised after the fact ────────────────────────────────

def test_risksmith_revenue_and_retained_headings_are_amounts():
    for h in ("Retained Commisson + Fronting", "Risksmith Retained Revenue",
              "50% of Column AW DUE PALMS. J/E FROM REVENUE"):
        assert itd._is_money_heading(itd._norm_name(h)), h
    assert not itd._is_money_heading(itd._norm_name("risksmith RETAINED Commission %"))


def test_automatic_settings_take_up_newly_recognised_amount_columns():
    REV = "Retained Revenue"
    rows = [{**_row(1), REV: 500.0}]
    auto = itd.normalize_config({**CFG, "auto_filled": True})
    got = itd.refresh_money_columns(auto, _landing(rows))
    assert REV in got["sheets"][SHEET]["money_columns"]
    # a person's own settings are left as they are
    assert REV not in itd.refresh_money_columns(CFG, _landing(rows))["sheets"][SHEET]["money_columns"]


def test_a_column_recognised_later_is_compared_with_what_was_delivered():
    # Delivered while "Retained Revenue" was not yet an amount column; the
    # premium then moves by 2,000 and the revenue does not. The adjustment
    # carries +2,000 and 0 revenue — not the whole 500 as if it were new.
    REV = "Retained Revenue"
    july = [{**_row(1, gwp=1000.0), REV: 500.0}]
    pos = _apply(itd.compare(_landing(july), CFG, {}, load_type=itd.ITD)["results"], {})
    aug = [{**july[0], GWP: 3000.0}]
    cfg = itd.refresh_money_columns(itd.normalize_config({**CFG, "auto_filled": True}),
                                    _landing(aug))
    c = itd.compare(_landing(aug), cfg, pos, load_type=itd.ITD)
    (r,) = [x for x in c["results"] if x["result"] == itd.CHANGED]
    assert r["delta"] == {GWP: 2000.0}
    adj = itd.build_delta(_landing(aug), cfg, c, load_type=itd.ITD)["sheets"][SHEET]["rows"][0]
    assert adj[GWP] == 2000.0 and adj[REV] == 0.0
