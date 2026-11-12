"""Plan task 35 — Financial Summary & Trends (financial_figures.py,
financial_summary.py): reading by heading, the five labels, the approved
rules (signs, % convention, the two identities), and the windows. Pure: files
are built in memory and fed in; no database, no `main`."""
from __future__ import annotations

import io
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
# exporter (whose reference-tab test the reader shares) builds an AI client at
# import; nothing here calls it.
os.environ.setdefault("GEMINI_API_KEY", "test-placeholder")

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

import exception_tally
import financial_figures as ff
import financial_summary as fs
import policy_search as ps
from auth_deps import Principal

CARRIER = Principal(user_id=1, tenant_id=7, role="carrier_admin")
BROKER = Principal(user_id=2, tenant_id=None, role="broker_admin", broker_party_id=50)
KAVACHIO = Principal(user_id=3, tenant_id=None, role="kavachio_admin")

PT = "premium_transaction_"
COLUMNS = [
    ("Policy No", "policy_number"),
    ("Transaction", PT + "type"),
    ("Gross Premium", PT + "gross_premium_amount"),
    ("100% GWP", PT + "total_gross_written_premium_amount"),
    ("Commission %", PT + "commission_percent"),
    ("Commission Amount", PT + "commission_amount"),
    ("Brokerage", PT + "brokerage_amount"),
    ("Net Premium", PT + "net_premium_amount"),
    ("Currency", PT + "original_currency"),
    ("Inception", "policy_effective_date"),
    ("Expiry", "policy_expiration_date"),
    ("Notes", None),
]


def structure(columns=COLUMNS, **marks):
    """A one-sheet output template; `marks` = {heading: column extras}."""
    return {"sheets": [{"sheet_name": "BDX", "data_start_row": 1, "columns": [
        {"column_index": i, "column_name": h, "canonical_field": f, **marks.get(h, {})}
        for i, (h, f) in enumerate(columns)]}]}


def xlsx(headings, rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "BDX"
    ws.append(headings)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def full_row(policy="P1", txn="New", gross=1000, total=4000, pct=10, comm=100,
             brok=None, net=900, cur="USD"):
    return [policy, txn, gross, total, pct, comm, brok, net, cur, "2026-01-01", "2027-01-01", "x"]


def read(rows, columns=COLUMNS, headings=None, **marks):
    heads = headings or [h for h, _f in columns]
    return ff.read_file(structure(columns, **marks), xlsx(heads, rows), "f.xlsx")


def figs(rows, columns=COLUMNS, **marks):
    fr = read(rows, columns, **marks)
    pooled = ff.pool([fr.evidence])
    conv = {n: ff.convention(pooled.get(n)) for n in ff.RATES}
    return [ff.figures(c, conv) for c in fr.rows]


# ── reading by heading ──────────────────────────────────────────────────────
def test_a_compacted_file_is_read_by_heading_not_position():
    """The writer drops empty columns: here "100% GWP", "Brokerage" and "Notes"
    are missing, so every later column has moved left."""
    heads = ["Policy No", "Transaction", "Gross Premium", "Commission %",
             "Commission Amount", "Net Premium", "Currency", "Inception", "Expiry"]
    data = xlsx(heads, [["P1", "New", 1000, 10, 100, 900, "USD", "2026-01-01", "2027-01-01"]])
    fr = ff.read_file(structure(), data, "f.xlsx")
    f = ff.figures(fr.rows[0], {"commission": ("whole", ""), "brokerage": ("whole", "")})
    assert f["gross_premium"]["value"] == 1000
    assert f["commission_amount"]["value"] == 100
    assert f["net_premium"]["value"] == 900
    assert f["currency"]["value"] == "USD"
    assert f["total_gwp"]["status"] == ff.MISSING
    assert fr.absent == 2          # "100% GWP" and "Brokerage" were not written


def test_a_row_without_a_policy_number_is_a_total_not_a_policy():
    fr = read([full_row(), [None, None, 1000, None, None, None, None, None, None, None, None, "Total"]])
    assert len(fr.rows) == 1


def test_a_file_whose_headings_match_nothing_says_so():
    fr = ff.read_file(structure(), xlsx(["a", "b"], [[1, 2]]), "f.xlsx")
    assert fr.rows == [] and "headings" in fr.problem


def test_no_stored_file_is_a_problem_not_a_zero():
    assert ff.read_file(structure(), None, "f.xlsx").problem


# ── the five labels ─────────────────────────────────────────────────────────
TWO_GROSS = COLUMNS[:3] + [("Inception Prem", PT + "gross_premium_amount")] + COLUMNS[3:]


def _two_gross_row(a, b):
    r = full_row(gross=a)
    return r[:3] + [b] + r[3:]


def test_two_columns_that_agree_are_reported():
    f = figs([_two_gross_row(1000, 1000)], TWO_GROSS)[0]
    assert f["gross_premium"] == {"value": 1000, "source": 1000, "status": ff.REPORTED, "how": None}


def test_two_columns_that_disagree_need_review_never_a_pick():
    f = figs([_two_gross_row(1000, 750)], TWO_GROSS)[0]
    assert f["gross_premium"]["status"] == ff.REVIEW and f["gross_premium"]["value"] is None
    assert "Gross Premium = 1000" in f["gross_premium"]["how"]
    assert "Inception Prem = 750" in f["gross_premium"]["how"]


def test_missing_is_not_reported_never_zero():
    f = figs([full_row(net=None)])[0]
    assert f["net_premium"] == {"value": None, "source": None, "status": ff.MISSING, "how": None}
    assert f["earned_premium"]["status"] == ff.MISSING


def test_text_in_a_money_column_needs_review():
    f = figs([full_row(net="n/a")])[0]
    assert f["net_premium"]["status"] == ff.REVIEW


def test_canonical_field_is_the_only_mapping():
    """Notes left on a column by the old summary-only corrections are ignored:
    the column feeds the figure its canonical_field names, nothing else."""
    cols = COLUMNS[:4] + [("Commission Rate", PT + "commission_amount")] + COLUMNS[5:]
    stale = {"Commission Rate": {"summary_field": PT + "commission_percent",
                                 "summary_review": "needs_review"},
             "100% GWP": {"summary_field": None}}
    f = figs([full_row(pct=10, comm=100)], cols, **stale)[0]
    # Both columns are tagged as the commission amount (10 and 100): read as
    # tagged, they disagree — the old note no longer moves one to the rate.
    assert f["commission_amount"]["status"] == ff.REVIEW
    assert "Commission Rate = 10" in f["commission_amount"]["how"]
    assert f["commission_pct"]["status"] == ff.MISSING
    assert f["total_gwp"]["value"] == 4000             # an emptied note no longer hides a column


# ── Add together ────────────────────────────────────────────────────────────
SECTIONS = COLUMNS[:3] + [("APD Premium", PT + "gross_premium_amount"),
                          ("MTC Premium", PT + "gross_premium_amount")] + COLUMNS[3:]
TICK = {"add_together": True}


def _sections(gross=None, apd=700, mtc=300, txn="New"):
    r = full_row(gross=gross, txn=txn)
    return r[:3] + [apd, mtc] + r[3:]


def test_ticked_columns_are_added_together():
    marks = {"Gross Premium": TICK, "APD Premium": TICK, "MTC Premium": TICK}
    f = figs([_sections(gross=0)], SECTIONS, **marks)[0]
    assert f["gross_premium"]["value"] == 1000 and f["gross_premium"]["status"] == ff.REPORTED
    assert "added together" in f["gross_premium"]["how"]
    assert "APD Premium 700.00 + MTC Premium 300.00" in f["gross_premium"]["how"]


def test_a_zero_part_counts_as_sent():
    marks = {"Gross Premium": TICK, "APD Premium": TICK, "MTC Premium": TICK}
    f = figs([_sections(gross=0, mtc=0)], SECTIONS, **marks)[0]
    assert f["gross_premium"]["value"] == 700 and f["gross_premium"]["status"] == ff.REPORTED


def test_a_missing_part_needs_review_never_a_smaller_total():
    marks = {"Gross Premium": TICK, "APD Premium": TICK, "MTC Premium": TICK}
    f = figs([_sections(gross=0, mtc=None)], SECTIONS, **marks)[0]
    assert f["gross_premium"]["status"] == ff.REVIEW and f["gross_premium"]["value"] is None
    assert "MTC Premium" in f["gross_premium"]["how"]


def test_a_ticked_column_not_in_the_file_is_a_missing_part():
    marks = {"Gross Premium": TICK, "APD Premium": TICK, "MTC Premium": TICK}
    heads = [h for h, _f in SECTIONS if h != "MTC Premium"]
    row = [v for (h, _f), v in zip(SECTIONS, _sections(gross=0)) if h != "MTC Premium"]
    fr = ff.read_file(structure(SECTIONS, **marks), xlsx(heads, [row]), "f.xlsx")
    f = ff.figures(fr.rows[0], {"commission": ("whole", ""), "brokerage": ("whole", "")})
    assert f["gross_premium"]["status"] == ff.REVIEW


def test_every_part_empty_is_not_reported():
    marks = {"Gross Premium": TICK, "APD Premium": TICK, "MTC Premium": TICK}
    f = figs([_sections(gross=None, apd=None, mtc=None)], SECTIONS, **marks)[0]
    assert f["gross_premium"]["status"] == ff.MISSING


def test_ticked_on_some_columns_only_needs_review():
    """A total and its parts must never both be counted."""
    marks = {"APD Premium": TICK, "MTC Premium": TICK}
    f = figs([_sections(gross=1000)], SECTIONS, **marks)[0]
    assert f["gross_premium"]["status"] == ff.REVIEW
    assert "tick all of them or none" in f["gross_premium"]["how"]


def test_unticked_columns_must_still_agree():
    f = figs([_sections(gross=1000)], SECTIONS)[0]
    assert f["gross_premium"]["status"] == ff.REVIEW      # 1000, 700 and 300 disagree


def test_text_in_a_ticked_part_needs_review():
    marks = {"Gross Premium": TICK, "APD Premium": TICK, "MTC Premium": TICK}
    f = figs([_sections(gross=0, mtc="n/a")], SECTIONS, **marks)[0]
    assert f["gross_premium"]["status"] == ff.REVIEW and "not a number" in f["gross_premium"]["how"]


def test_a_cancellation_makes_each_part_negative_before_adding():
    marks = {"Gross Premium": TICK, "APD Premium": TICK, "MTC Premium": TICK}
    f = figs([_sections(gross=0, apd=700, mtc=-300, txn="CN")], SECTIONS, **marks)[0]
    assert f["gross_premium"]["value"] == -1000           # −700 + −300, not −(700 − 300)
    assert f["gross_premium"]["source"] == 400            # the parts as sent


def test_a_switched_off_column_is_not_a_part():
    marks = {"Gross Premium": TICK, "APD Premium": TICK, "MTC Premium": {**TICK, "active": False}}
    heads = [h for h, _f in SECTIONS if h != "MTC Premium"]
    row = [v for (h, _f), v in zip(SECTIONS, _sections(gross=300)) if h != "MTC Premium"]
    fr = ff.read_file(structure(SECTIONS, **marks), xlsx(heads, [row]), "f.xlsx")
    f = ff.figures(fr.rows[0], {"commission": ("whole", ""), "brokerage": ("whole", "")})
    assert f["gross_premium"]["value"] == 1000


def test_rates_are_never_added_together():
    cols = COLUMNS[:5] + [("Broker Commission %", PT + "commission_percent")] + COLUMNS[5:]
    marks = {"Commission %": TICK, "Broker Commission %": TICK}
    r = full_row(pct=10)
    f = figs([r[:5] + [5] + r[5:]], cols, **marks)[0]
    assert f["commission_pct"]["status"] == ff.REVIEW     # they disagree; never 15%


def test_the_field_editor_carries_the_tick():
    import output_template_fields as otf
    st = otf.complete_structure(structure(SECTIONS))
    flat = otf.flatten(st)
    assert all(f["add_together"] is False for f in flat)
    apd = next(f for f in flat if f["column_name"] == "APD Premium")
    st2, errors = otf.apply_edits(st, [{**f, "add_together": f is apd} for f in flat])
    assert not errors
    col = next(c for c in st2["sheets"][0]["columns"] if c["column_name"] == "APD Premium")
    assert col["add_together"] is True
    # Fields never ticked gain no key.
    assert all("add_together" not in c for c in st2["sheets"][0]["columns"] if c["column_name"] != "APD Premium")


# ── the latest version of a file's template ────────────────────────────────
def _v(id, version, *, tenant=7, name="Fleet BDX", sample="s1", fmt="xlsx",
       carrier=None, program=11, broker=50, contract=900):
    return SimpleNamespace(id=id, version=version, tenant_id=tenant, name=name, sample=sample,
                           output_format=fmt, carrier_party_id=carrier, program_id=program,
                           broker_party_id=broker, contract_id=contract)


def test_a_file_is_read_with_the_newest_fork_of_its_template():
    latest = fs.latest_of([_v(1, 1), _v(2, 2), _v(3, 3)])
    assert latest == {1: 3, 2: 3, 3: 3}


def test_never_an_older_version():
    assert fs.latest_of([_v(5, 2), _v(4, 1)])[5] == 5


def test_a_new_sample_under_the_same_name_is_not_a_fork():
    latest = fs.latest_of([_v(1, 1), _v(2, 2, sample="s2")])
    assert latest == {1: 1, 2: 2}


@pytest.mark.parametrize("other", [dict(contract=901), dict(broker=51), dict(program=12),
                                   dict(tenant=8), dict(name="Cargo BDX"), dict(fmt="csv")])
def test_a_different_scope_carrier_or_name_is_not_a_fork(other):
    latest = fs.latest_of([_v(1, 1), _v(2, 2, **other)])
    assert latest[1] == 1


def test_scope_filled_in_later_still_matches_its_forks():
    """The scope columns were filled in on older rows after their forks were
    made; an unset side matches."""
    latest = fs.latest_of([_v(1, 1), _v(2, 2, program=None, broker=None, contract=None)])
    assert latest[1] == 2


# ── signs ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("code", ["CN", "Cancellation", "cancellation", "FC", "Flat Cancellation",
                                  "Endorsement - Return Premium", "Endorsement – Return Premium"])
def test_approved_cancellation_and_return_codes_count_negative(code):
    f = figs([full_row(txn=code, brok=20)])[0]
    assert f["transaction"]["negative"] is True
    for k, sent in (("gross_premium", 1000), ("total_gwp", 4000), ("commission_amount", 100),
                    ("brokerage_amount", 20), ("net_premium", 900)):
        assert f[k]["value"] == -sent and f[k]["source"] == sent, k
    assert f["commission_pct"]["value"] == 10           # a rate keeps its value


def test_an_amount_already_negative_stays_negative():
    f = figs([full_row(txn="CN", gross=-1000)])[0]
    assert f["gross_premium"]["value"] == -1000 and f["gross_premium"]["source"] == -1000


@pytest.mark.parametrize("code", ["ON", "OF", "C", "Audit", "Endorsement - Additional Premium"])
def test_codes_not_on_the_approved_list_keep_their_sign(code):
    f = figs([full_row(txn=code)])[0]
    assert f["transaction"]["negative"] is False
    assert f["gross_premium"]["value"] == 1000


def test_on_and_of_are_left_unclassified():
    assert ff.classify("ON")["class"] == "unclassified"
    assert ff.classify("OF")["class"] == "unclassified"
    assert ff.classify("NB")["class"] == "new"
    assert ff.classify("Endorsement - Return Premium")["class"] == "return_premium"


# ── percentage convention ───────────────────────────────────────────────────
def test_whole_percent_is_proved_by_amount_over_premium():
    f = figs([full_row(pct=23.5, comm=235)])[0]
    assert f["commission_pct"]["value"] == 23.5 and f["commission_pct"]["status"] == ff.REPORTED


def test_a_fraction_is_converted_only_when_the_rows_prove_it():
    f = figs([full_row(pct=0.235, comm=235)])[0]
    assert f["commission_pct"]["value"] == pytest.approx(23.5)
    assert f["commission_pct"]["source"] == 0.235


def test_an_unprovable_small_rate_needs_review():
    f = figs([full_row(pct=0.25, comm=None, gross=None)])[0]
    assert f["commission_pct"]["status"] == ff.REVIEW and f["commission_pct"]["value"] is None


def test_a_rate_written_with_a_percent_sign_is_whole():
    f = figs([full_row(pct="0.5%", comm=None, gross=None)])[0]
    assert f["commission_pct"]["value"] == 0.5


def test_values_above_one_cannot_be_fractions():
    f = figs([full_row(pct=15, comm=None, gross=None), full_row("P2", pct=0.5, comm=None, gross=None)])
    assert f[1]["commission_pct"]["value"] == 0.5 and f[1]["commission_pct"]["status"] == ff.REPORTED


# ── the two identities ──────────────────────────────────────────────────────
def test_commission_amount_is_calculated_from_gross_and_rate():
    f = figs([full_row(pct=12.5, comm=None), full_row("P2", pct=10, comm=100)])[0]
    assert f["commission_amount"]["status"] == ff.CALCULATED
    assert f["commission_amount"]["value"] == 125
    assert "1,000.00 × 12.5%" in f["commission_amount"]["how"]


def test_commission_rate_is_calculated_from_amount_and_gross():
    f = figs([full_row(pct=None, comm=150)])[0]
    assert f["commission_pct"]["status"] == ff.CALCULATED
    assert f["commission_pct"]["value"] == 15


def test_a_calculated_amount_on_a_cancellation_is_negative():
    f = figs([full_row(txn="CN", pct=10, comm=None), full_row("P2", pct=10, comm=100)])[0]
    assert f["commission_amount"]["value"] == -100


def test_net_and_earned_are_never_worked_out():
    f = figs([full_row(net=None)])[0]
    assert f["net_premium"]["status"] == ff.MISSING
    assert f["earned_premium"]["status"] == ff.MISSING


def test_an_input_needing_review_stops_the_calculation():
    f = figs([_two_gross_row(1000, 750)[:6] + [None] + _two_gross_row(1000, 750)[7:]], TWO_GROSS)[0]
    assert f["commission_amount"]["status"] == ff.REVIEW


# ── the summary ─────────────────────────────────────────────────────────────
def export(eid, period, rows, *, tenant=7, program=11, broker=50, content=None):
    return SimpleNamespace(id=eid, tenant_id=tenant, program_id=program, broker_party_id=broker,
                           contract_id=900, template_id=1, filename=f"file-{eid}.xlsx",
                           blob_ref=None, blob_md5=content or f"md5-{eid}",
                           created_at=None, _period=period, _rows=rows)


@pytest.fixture
def files(monkeypatch):
    box = SimpleNamespace(exports=[], open={})
    fs._REMEMBERED.clear()

    def _exports(s, seat, carrier_id=None):
        return [o for o in box.exports
                if (seat.kind == "carrier" and o.tenant_id == seat.tenant_id)
                or (seat.kind == "broker" and o.broker_party_id == seat.broker_party_id
                    and (carrier_id is None or o.tenant_id == carrier_id))]

    monkeypatch.setattr(ps, "_exports", _exports)
    monkeypatch.setattr(ps, "_structures", lambda s, ids: {1: structure()})
    monkeypatch.setattr(fs, "_latest_templates", lambda s, ids: {i: i for i in ids})
    monkeypatch.setattr(ps, "_names", lambda s, ex: {
        "programme": {11: "Fleet", 12: "Cargo"}, "broker": {50: "Wani", 51: "Other"},
        "insurer": {7: "Carrier Co", 8: "Second Carrier"}})
    monkeypatch.setattr(exception_tally, "export_versions",
                        lambda s, ids: {o.id: {"reporting_period": o._period} for o in box.exports})
    monkeypatch.setattr(fs, "_bytes", lambda s, o: xlsx([h for h, _f in COLUMNS], o._rows))
    monkeypatch.setattr(fs, "_open_counts", lambda s, ids: {i: box.open.get(i, 0) for i in ids})
    monkeypatch.setattr(ps, "seat_of", _seat_of)
    return box


def _seat_of(s, p):
    if p.role == "broker_admin":
        return ps.Seat("broker", broker_party_id=p.broker_party_id, program_ids={11, 12})
    if p.role == "carrier_admin":
        return ps.Seat("carrier", tenant_id=p.tenant_id)
    raise HTTPException(403, "Policy search is for carriers and brokers.")


def _totals(out, currency="USD"):
    return next(g for g in out["groups"] if g["currency"] == currency)["totals"]


def test_windows_compare_the_period_before_and_last_year(files):
    files.exports = [export(1, "2027-03", [full_row(gross=1200)]),
                     export(2, "2027-02", [full_row(gross=1000)]),
                     export(3, "2026-03", [full_row(gross=800)])]
    out = fs.summary(None, CARRIER)
    assert out["period"] == "2027-03"
    g = _totals(out)["gross_premium"]
    assert (g["current"]["value"], g["previous"]["value"], g["last_year"]["value"]) == (1200, 1000, 800)
    assert g["change"] == {"previous": 20.0, "last_year": 50.0}


def test_a_rate_changes_in_points_and_is_total_over_total(files):
    files.exports = [export(1, "2027-03", [full_row(gross=1000, comm=150), full_row("P2", gross=1000, comm=50)]),
                     export(2, "2027-02", [full_row(gross=1000, comm=80)])]
    m = _totals(fs.summary(None, CARRIER))["commission_pct"]
    assert m["current"]["value"] == 10.0          # 200 ÷ 2,000
    assert m["change"]["previous"] == 2.0          # 10% against 8%: two points


def test_nothing_to_compare_with_is_no_change_not_infinity(files):
    files.exports = [export(1, "2027-03", [full_row()])]
    assert _totals(fs.summary(None, CARRIER))["gross_premium"]["change"] == {"previous": None, "last_year": None}


def test_currencies_are_never_added_together(files):
    files.exports = [export(1, "2027-03", [full_row(gross=1000, cur="USD"),
                                           full_row("P2", gross=500, cur="GBP"),
                                           full_row("P3", gross=70, cur=None)])]
    out = fs.summary(None, CARRIER)
    assert {g["currency"]: g["totals"]["gross_premium"]["current"]["value"] for g in out["groups"]} \
        == {"USD": 1000, "GBP": 500, fs.NOT_STATED: 70}


def test_cancellations_reduce_the_total(files):
    files.exports = [export(1, "2027-03", [full_row(gross=1000), full_row("P2", txn="CN", gross=300)])]
    assert _totals(fs.summary(None, CARRIER))["gross_premium"]["current"]["value"] == 700


def test_a_figure_no_row_reports_is_none_with_its_counts(files):
    files.exports = [export(1, "2027-03", [full_row(net=None)])]
    m = _totals(fs.summary(None, CARRIER))["net_premium"]["current"]
    assert m["value"] is None and m["counts"][ff.MISSING] == 1


def test_by_programme_and_by_broker(files):
    files.exports = [export(1, "2027-03", [full_row(gross=1000)], program=11, broker=50),
                     export(2, "2027-03", [full_row(gross=400)], program=12, broker=51)]
    g = fs.summary(None, CARRIER)["groups"][0]
    assert [(p["name"], p["totals"]["gross_premium"]["current"]["value"]) for p in g["by_programme"]] \
        == [("Fleet", 1000), ("Cargo", 400)]
    assert [b["name"] for b in g["by_broker"]] == ["Wani", "Other"]


def test_filters_narrow_before_adding_up(files):
    files.exports = [export(1, "2027-03", [full_row(gross=1000)], program=11),
                     export(2, "2027-03", [full_row(gross=400)], program=12)]
    assert _totals(fs.summary(None, CARRIER, program_id=12))["gross_premium"]["current"]["value"] == 400


def test_an_identical_file_counts_once(files):
    files.exports = [export(1, "2027-03", [full_row(gross=1000)], content="same"),
                     export(2, "2027-03", [full_row(gross=1000)], content="same")]
    out = fs.summary(None, CARRIER)
    assert _totals(out)["gross_premium"]["current"]["value"] == 1000
    assert [c["export_id"] for c in out["coverage"]["copies"]] == [1]


def test_a_quarterly_file_is_in_its_quarter_not_a_month(files):
    files.exports = [export(1, "2027-Q1", [full_row(gross=900)]),
                     export(2, "2027-03", [full_row(gross=100)])]
    month = fs.summary(None, CARRIER, period="2027-03")
    assert _totals(month)["gross_premium"]["current"]["value"] == 100
    assert [f["export_id"] for f in month["coverage"]["longer_period"]] == [1]
    quarter = fs.summary(None, CARRIER, basis="quarter", period="2027-Q1")
    assert _totals(quarter)["gross_premium"]["current"]["value"] == 1000


def test_a_file_with_no_period_is_listed_not_counted(files):
    files.exports = [export(1, None, [full_row(gross=900)]), export(2, "2027-03", [full_row(gross=100)])]
    out = fs.summary(None, CARRIER)
    assert _totals(out)["gross_premium"]["current"]["value"] == 100
    assert [f["export_id"] for f in out["coverage"]["no_period"]] == [1]


def test_files_with_open_problems_can_be_left_out(files):
    files.exports = [export(1, "2027-03", [full_row(gross=1000)]), export(2, "2027-03", [full_row("P2", gross=50)])]
    files.open = {2: 3}
    assert _totals(fs.summary(None, CARRIER))["gross_premium"]["current"]["value"] == 1050
    out = fs.summary(None, CARRIER, open_problems="exclude")
    assert _totals(out)["gross_premium"]["current"]["value"] == 1000
    assert out["coverage"]["open_problems"] == {"files": 0, "excluded": True}


def test_a_broker_sees_only_its_own_files(files):
    files.exports = [export(1, "2027-03", [full_row(gross=1000)], broker=50),
                     export(2, "2027-03", [full_row(gross=400)], broker=51)]
    out = fs.summary(None, BROKER)
    assert _totals(out)["gross_premium"]["current"]["value"] == 1000
    assert out["groups"][0]["by_broker"] == []


def test_kavachio_is_refused(files):
    with pytest.raises(HTTPException) as e:
        fs.summary(None, KAVACHIO)
    assert e.value.status_code == 403


def test_net_written_premium_stays_not_available(files):
    files.exports = [export(1, "2027-03", [full_row()])]
    assert fs.summary(None, CARRIER)["net_written_premium"]["status"] == "not_available"


def test_rows_carry_every_label_and_the_amount_as_sent(files):
    files.exports = [export(1, "2027-03", [full_row(txn="CN", pct=10, comm=None), full_row("P2")])]
    out = fs.rows(None, CARRIER, status=ff.CALCULATED)
    assert out["total"] == 1
    item = out["items"][0]
    assert item["figures"]["commission_amount"]["status"] == ff.CALCULATED
    assert item["figures"]["gross_premium"] == {
        "value": -1000, "source": 1000, "status": ff.REPORTED,
        "how": "sent as 1,000.00 on a CN row, counted as -1,000.00"}


def test_a_bad_period_or_basis_is_refused(files):
    with pytest.raises(HTTPException):
        fs.summary(None, CARRIER, basis="week")
    with pytest.raises(HTTPException):
        fs.summary(None, CARRIER, basis="month", period="2027-Q1")


def test_windows_shift_across_years():
    assert fs.windows("2027-01", "month") == {"current": "2027-01", "previous": "2026-12", "last_year": "2026-01"}
    assert fs.windows("2027-Q1", "quarter") == {"current": "2027-Q1", "previous": "2026-Q4", "last_year": "2026-Q1"}
    assert fs.windows("2027", "year") == {"current": "2027", "previous": "2026", "last_year": "2026"}
