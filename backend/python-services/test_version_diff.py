"""Plan tasks 23.2–23.4 — this version against the last accepted version
(version_diff.py). Pure: no database."""
from __future__ import annotations

import version_diff as vd

COLS = ["Policy Number", "Transaction Type", "Transaction Effective Date",
        "Reporting Period", "Insured Name", "Gross Premium", "Commission %", "Net Premium"]
SHEET = "BDX"


def r(pol, typ, eff, gross, *, name="Acme", comm=10, net=None, period="2025-08"):
    return {"Policy Number": pol, "Transaction Type": typ,
            "Transaction Effective Date": eff, "Reporting Period": period,
            "Insured Name": name, "Gross Premium": gross, "Commission %": comm,
            "Net Premium": gross * 0.9 if net is None else net}


def land(*rows, cols=COLS, sheet=SHEET):
    return {"sheets": {sheet: {"columns": list(cols), "rows": [dict(x) for x in rows]}}}


# What a Bordereau Setup tells the comparison: which data-model field each
# input column is mapped to, the columns it reads, and the reported-period
# column of its ITD settings. Nothing is guessed from a heading.
MAPPING = {"Policy Number": "policy_number", "Transaction Type": "premium_transaction_type",
           "Transaction Effective Date": "premium_transaction_effective_date",
           "Insured Name": "policyholder_legal_name",
           "Gross Premium": "premium_transaction_gross_premium_amount",
           "Commission %": "premium_transaction_commission_percent",
           "Net Premium": "premium_transaction_net_premium_amount"}


def setup(cols=COLS, **over):
    out = {"canonical_of": {c: f for c, f in MAPPING.items() if c in cols},
           "mapped_columns": [c for c in cols],
           "period_column": "Reporting Period" if "Reporting Period" in cols else None}
    out.update(over)
    return out


def compare(base: dict, cur: dict, cols=COLS, **kw):
    kw.setdefault("accepted_setup", setup(cols))
    kw.setdefault("current_setup", setup(cols))
    return vd.diff_versions(base, cur, **kw)


BASE = [r("P1", "New Business", "2025-08-01", 1000),
        r("P2", "New Business", "2025-08-03", 2000),
        r("P3", "Renewal", "2025-08-05", 3000)]


def diff(base, cur, **kw):
    return compare(land(*base), land(*cur), **kw)


def only(res, outcome):
    return [x for x in res["rows"] if x["outcome"] == outcome]


def test_nothing_moved():
    res = diff(BASE, BASE)
    assert res["state"] == "compared"
    assert res["summary"]["outcomes"]["unchanged"] == 3
    assert res["rows"] == []
    assert vd.OUTCOMES == ("new", "changed", "disappeared", "restated", "unchanged")


# ── the six-case proof of the purpose sentence (23.3) ───────────────────────

def test_1_premium_changed_in_place_is_a_silent_restatement():
    cur = [BASE[0], r("P2", "New Business", "2025-08-03", 2500), BASE[2]]
    res = diff(BASE, cur)
    (x,) = only(res, "restated")
    assert x["restatement"] == "silent"
    assert x["money"]["Gross Premium"] == {"was": 2000.0, "now": 2500.0, "change": 500.0}
    assert res["summary"]["silent_restatements"] == 1


def test_2_an_endorsement_row_is_a_genuine_endorsement_not_a_restatement():
    cur = [*BASE, r("P2", "Endorsement - Additional Premium", "2025-08-20", 500)]
    res = diff(BASE, cur)
    assert only(res, "restated") == []
    (x,) = only(res, "new")
    assert x["kind"] == "endorsement"
    assert res["summary"]["genuine_endorsements"] == 1


def test_3_both_at_once_is_still_silent_and_flagged_for_double_count():
    cur = [BASE[0], r("P2", "New Business", "2025-08-03", 2500), BASE[2],
           r("P2", "Endorsement", "2025-08-20", 500)]
    res = diff(BASE, cur)
    (x,) = only(res, "restated")
    assert x["restatement"] == "silent"
    assert "endorsement_added_same_policy" in x["flags"]
    assert "counted twice" in x["detail"]


def test_4_a_cancellation_row_declares_the_change():
    cur = [BASE[0], r("P2", "New Business", "2025-08-03", 2500), BASE[2],
           r("P2", "Cancellation", "2025-08-03", -500)]
    res = diff(BASE, cur)
    (x,) = only(res, "restated")
    assert x["restatement"] == "declared"
    (n,) = only(res, "new")
    assert n["kind"] == "cancellation" and n.get("explains")
    # the plain words point at each other's rows
    assert f"(row {n['row']}) explains it (declared restatement)" in x["detail"]
    assert n["detail"] == ("New row, transaction type 'Cancellation': it explains the amount "
                           f"change on row {x['row']}.")


def test_5_original_gone_and_a_cancellation_added_is_linked():
    cur = [BASE[0], BASE[2], r("P2", "Cancellation", "2025-08-03", -2000)]
    res = diff(BASE, cur)
    (gone,) = only(res, "disappeared")
    assert "replaced_by_new_row" in gone["flags"]
    (n,) = only(res, "new")
    assert n["kind"] == "cancellation"


def test_6_an_amount_equal_to_a_recorded_fix_is_corrected_on_request():
    cur = [BASE[0], r("P2", "New Business", "2025-08-03", 2500, net=1800), BASE[2]]
    fix = {"sheet": SHEET, "column": "Gross Premium", "value": "2500",
           "values": {"policy": "p2", "type": "new business", "date": "2025-08-03",
                      "period": "2025-08"}}
    res = diff(BASE, cur, corrections=[fix])
    (x,) = only(res, "restated")
    assert x["restatement"] == "corrected_on_request"
    assert res["summary"]["silent_restatements"] == 0
    # a second amount moved with no fix recorded for it: silent after all
    cur = [BASE[0], r("P2", "New Business", "2025-08-03", 2500, net=2000), BASE[2]]
    (x,) = only(diff(BASE, cur, corrections=[fix]), "restated")
    assert x["restatement"] == "silent"


# ── 23.2 outcomes ────────────────────────────────────────────────────────────

def test_new_disappeared_and_changed_rows():
    cur = [r("P1", "New Business", "2025-08-01", 1000, name="Acme Ltd"),
           BASE[1], r("P9", "New Business", "2025-08-09", 900)]
    res = diff(BASE, cur)
    (ch,) = only(res, "changed")
    assert ch["changed_fields"] == ["Insured Name"]
    assert ch["changes"]["Insured Name"] == {"was": "Acme", "now": "Acme Ltd"}
    (gone,) = only(res, "disappeared")
    assert gone["policy"] == "P3"
    (new,) = only(res, "new")
    assert new["policy"] == "P9" and new["kind"] == "new"
    # the insured is shown on every row (taken from the new value), and still compared
    assert ch["insured"] == "Acme Ltd" and gone["insured"] == "Acme"
    assert ch["detail"] == "Details changed (no amounts): Insured Name."
    assert gone["detail"] == "Was in the accepted version, is not in this file."
    assert new["detail"] == "New row, transaction type 'New Business'."
    o = res["summary"]["outcomes"]
    assert (o["new"], o["changed"], o["disappeared"], o["restated"], o["unchanged"]) == (1, 1, 1, 0, 1)


def test_commission_percent_and_net_premium_are_amounts():
    cur = [r("P1", "New Business", "2025-08-01", 1000, comm=12.5), BASE[1],
           r("P3", "Renewal", "2025-08-05", 3000, net=2600)]
    res = diff(BASE, cur)
    got = {x["policy"]: set(x["money"]) for x in only(res, "restated")}
    assert got == {"P1": {"Commission %"}, "P3": {"Net Premium"}}


def test_formatting_is_not_a_change():
    cur = [{**BASE[0], "Gross Premium": "1,000.00", "Transaction Effective Date": "01/08/2025",
            "Policy Number": " p1 "},
           {**BASE[1], "Net Premium": "1800"}, {**BASE[2], "Insured Name": "  Acme "}]
    res = diff(BASE, cur)
    assert res["rows"] == [] and res["summary"]["outcomes"]["unchanged"] == 3


# ── rows without a key (no policy number / no effective date) ──────────────

def test_rows_without_a_key_are_new_or_disappeared_unless_identical():
    cur = [*BASE, r("", "New Business", "2025-08-09", 50), r("P7", "New Business", None, 70),
           r("", "Renewal", "2025-08-11", 60)]
    base = [*BASE, r("P8", "New Business", "", 80), r("", "Renewal", "2025-08-11", 60)]
    res = diff(base, cur)
    assert {x["outcome"] for x in res["rows"]} == {"new", "disappeared"}
    new, gone = only(res, "new"), only(res, "disappeared")
    assert {tuple(x["missing"]) for x in new} == {("Policy number",),
                                                 ("Transaction effective date",)}
    assert [x["missing"] for x in gone] == [["Transaction effective date"]]
    assert all("no_key" in x["flags"] for x in res["rows"])
    # the keyless row present, identical, in both versions is unchanged
    assert res["summary"]["outcomes"]["unchanged"] == 4
    assert (5, 4) in res["view"][SHEET]["matched"]          # this row 6 = accepted row 5
    (p,) = [x for x in new if x["missing"] == ["Policy number"]]
    assert p["detail"] == ("New row, transaction type 'New Business'. It has no policy number, "
                           "so it could only be matched to an identical row, and the accepted "
                           "version has none.")
    assert gone[0]["detail"].endswith("and this file has none.")


def test_a_sheet_without_a_policy_column_is_not_compared():
    cols = [c for c in COLS if c != "Policy Number"]
    rows = [{k: v for k, v in x.items() if k != "Policy Number"} for x in BASE]
    res = compare(land(*rows, cols=cols), land(*rows, cols=cols), cols=cols)
    assert res["state"] == "not_compared"
    assert res["sheets"][0]["state"] == "key_column_missing"
    assert "policy number" in res["summary"]["plain"]


def test_a_key_column_the_setup_does_not_map_is_never_guessed():
    """Every heading is there, but the setup maps the effective date to the
    POLICY effective date: the sheet is not compared and says what to map."""
    mapping = {**setup()["canonical_of"],
               "Transaction Effective Date": "policy_effective_date"}
    res = diff(BASE, BASE, accepted_setup=setup(canonical_of=mapping),
               current_setup=setup(canonical_of=mapping))
    assert res["state"] == "not_compared"
    assert res["summary"]["plain"] == (
        "Not compared — the setup maps no column to transaction effective date "
        "(the accepted version and this version). Map it in Bordereau Setup.")
    assert diff(BASE, BASE, accepted_setup={}, current_setup={})["state"] == "not_compared"


def test_the_setups_date_format_reads_the_effective_date():
    us = [{**x, "Transaction Effective Date": f"08/{x['Transaction Effective Date'][-2:]}/2025"}
          for x in BASE]
    fmt = {"Transaction Effective Date": "%m/%d/%Y"}
    res = diff(BASE, us, current_setup=setup(date_formats=fmt))
    assert res["rows"] == [] and res["summary"]["outcomes"]["unchanged"] == 3


# ── optional parts: "go with whatever we have" ──────────────────────────────

def test_without_a_type_column_the_key_is_policy_date_period():
    cols = [c for c in COLS if c != "Transaction Type"]
    base = [{k: v for k, v in x.items() if k != "Transaction Type"} for x in BASE]
    cur = [dict(base[0]), {**base[1], "Gross Premium": 2500}, dict(base[2])]
    res = compare(land(*base, cols=cols), land(*cur, cols=cols), cols=cols)
    assert res["sheets"][0]["parts_used"] == ["policy", "date", "period"]
    (x,) = only(res, "restated")
    assert x["restatement"] == "in_place_policy_level"


def test_a_blank_type_on_one_side_is_matched_on_the_reduced_key():
    cur = [BASE[0], {**BASE[1], "Transaction Type": ""}, BASE[2]]
    res = diff(BASE, cur)
    (x,) = res["rows"]
    assert x["outcome"] == "unchanged" and "matched_on_reduced_key" in x["flags"]


def test_the_reduced_key_needs_exactly_one_candidate():
    base = [r("P2", "New Business", "2025-08-03", 2000), r("P2", "Renewal", "2025-08-03", 2000)]
    cur = [r("P2", "", "2025-08-03", 2000)]
    res = diff(base, cur)
    assert [x["outcome"] for x in res["rows"]].count("new") == 1
    assert [x["outcome"] for x in res["rows"]].count("disappeared") == 2


# ── duplicate keys (D15) ────────────────────────────────────────────────────

def test_identical_twins_pair_exactly():
    twin = r("P5", "Endorsement", "2025-08-10", 100)
    res = diff([*BASE, twin, twin], [*BASE, twin, twin])
    assert res["rows"] == []


def test_same_key_rows_pair_by_amount_before_order():
    a, b = (r("P5", "Instalment", "2025-08-10", 100, name="first"),
            r("P5", "Instalment", "2025-08-10", 200, name="second"))
    # reordered and renamed: amounts still pair them correctly
    cur = [{**b, "Insured Name": "second x"}, {**a, "Insured Name": "first x"}]
    res = diff([a, b], cur)
    assert {x["outcome"] for x in res["rows"]} == {"changed"}
    assert all(not x["money"] for x in res["rows"])
    assert all("duplicate_key" in x["flags"] for x in res["rows"])


def test_same_key_rows_are_paired_with_the_closest_match():
    a = r("P5", "Instalment", "2025-08-10", 100, name="first")
    b = r("P5", "Instalment", "2025-08-10", 200, name="second")
    # both amounts moved and the rows swapped places: the names still say which is which
    cur = [{**b, "Gross Premium": 260, "Net Premium": 234},
           {**a, "Gross Premium": 150, "Net Premium": 135}]
    res = diff([a, b], cur)
    assert {x["outcome"] for x in res["rows"]} == {"restated"}
    got = {x["accepted_row"]: x["row"] for x in res["rows"]}
    assert got == {1: 2, 2: 1}
    assert all("paired_by_closest_match" in x["flags"] for x in res["rows"])
    assert all("paired with the most similar one" in x["detail"] for x in res["rows"])


def test_an_extra_row_in_a_group_is_new():
    a, b = (r("P5", "Instalment", "2025-08-10", 100), r("P5", "Instalment", "2025-08-10", 200))
    cur = [r("P5", "Instalment", "2025-08-10", 150), r("P5", "Instalment", "2025-08-10", 260),
           r("P5", "Instalment", "2025-08-10", 300)]
    res = diff([a, b], cur)
    assert sorted(x["outcome"] for x in res["rows"]) == ["new", "restated", "restated"]
    (extra,) = only(res, "new")
    assert extra["row"] == 3 and "duplicate_key" in extra["flags"]
    assert "Other rows share this policy" in extra["detail"]


def test_an_extra_copy_next_to_exact_twins_is_new_and_flagged():
    twin = r("P5", "Endorsement", "2025-08-10", 100)
    res = diff([twin], [twin, r("P5", "Endorsement", "2025-08-10", 40)])
    (x,) = res["rows"]
    assert x["outcome"] == "new" and "duplicate_key" in x["flags"]


def test_a_reversal_is_not_paired_with_its_original():
    orig = r("P5", "New Business", "2025-08-10", 100)
    a2 = r("P5", "New Business", "2025-08-10", 300)
    rev = r("P5", "New Business", "2025-08-10", -100)
    res = diff([orig, a2], [orig, rev, a2])
    (x,) = res["rows"]
    assert x["outcome"] == "new" and x["money"]["Gross Premium"]["now"] == -100.0


# ── money totals, sheets, ITD ───────────────────────────────────────────────

def test_the_control_total_balances():
    cur = [BASE[0], r("P2", "New Business", "2025-08-03", 2500),
           r("P9", "New Business", "2025-08-09", 900), r("", "x", "2025-08-01", 5)]
    res = diff(BASE, cur)
    g = res["summary"]["money"]["Gross Premium"]
    assert (g["accepted"], g["this"]) == (6000.0, 4405.0)          # the keyless row counts too
    assert (g["restated"], g["new"], g["disappeared"]) == (500.0, 905.0, 3000.0)
    assert g["control_gap"] == 0


def test_an_itd_file_is_not_compared_here():
    cur = land(*BASE)
    cur["itd"] = {"load_type": "itd"}
    res = compare(land(*BASE), cur)
    assert res["state"] == "itd_file" and res["rows"] == []


def test_sheets_only_in_one_version_are_listed():
    base = land(*BASE)
    cur = land(*BASE)
    cur["sheets"]["Claims"] = {"columns": ["A"], "rows": [{"A": 1}]}
    res = compare(base, cur)
    states = {s["sheet"]: s["state"] for s in res["sheets"]}
    assert states == {SHEET: "compared", "Claims": "only_in_this"}


def test_a_renamed_single_sheet_is_still_compared():
    res = compare(land(*BASE, sheet="Aug"), land(*BASE, sheet="August"))
    assert res["state"] == "compared" and res["sheets"][0]["note"] == "sheet renamed"


def test_only_the_columns_the_setup_reads_are_compared():
    cur = [{**BASE[0], "Insured Name": "Someone else"},
           {**BASE[1], "Gross Premium": 9999, "Net Premium": 9999 * 0.9}, BASE[2]]
    reads = [c for c in COLS if c not in ("Insured Name", "Gross Premium", "Net Premium")]
    res = diff(BASE, cur, current_setup=setup(mapped_columns=reads))
    assert res["rows"] == []                       # nothing the setup reads moved
    res = diff(BASE, cur)
    assert len(only(res, "changed")) == 1 and len(only(res, "restated")) == 1


def test_amounts_are_the_data_models_money_fields():
    assert vd.is_money("Any heading", "premium_transaction_net_premium_amount")
    assert vd.is_money("Rate", "premium_transaction_exchange_rate")
    assert not vd.is_money("Gross Premium", None)             # no field, no amount
    assert not vd.is_money("Limit", "policy_limit_amount")    # not a premium transaction
    assert vd._is_rate("x", "premium_transaction_commission_percent")
    assert not vd._is_rate("Commission %", "premium_transaction_commission_amount")


def test_transaction_types_are_read_as_ingest_reads_them():
    """The system's own code list (ingester._TXN_TYPE_MAP): a code, or a code
    followed by more words; anything else is "other", blank is the default."""
    from ingester import _TXN_TYPE_MAP
    f = vd.type_family
    for code, value in _TXN_TYPE_MAP.items():
        assert f(code) == value and f(code.lower()) == value
    assert f("Endorsement - Additional Premium") == "endorsement"
    assert f("Cancellation - Flat") == "cancellation"
    assert f("") == "unknown" and f(None) == "unknown"
    assert f("Something") == "other" and f("Revised") == "other"


def test_plain_words():
    cur = [BASE[0], r("P2", "New Business", "2025-08-03", 2500), BASE[2],
           r("P2", "Endorsement", "2025-08-20", 500)]
    res = diff(BASE, cur)
    assert res["summary"]["plain"] == ("1 new (by type: 1 endorsement), "
                                       "1 restated (1 silent restatement), 2 unchanged.")
    assert diff(BASE, BASE)["summary"]["plain"] == (
        "All 3 rows are the same as in the accepted version.")
    cur = [r("P1", "New Business", "2025-08-01", 1000, name="Acme Ltd"), BASE[1],
           r("P9", "New Business", "2025-08-09", 900), r("", "New", "2025-08-09", 5)]
    assert diff(BASE, cur)["summary"]["plain"] == (
        "2 new (by type: 2 new), 1 changed, 1 disappeared, 1 unchanged.")
    assert vd.restated_parts(res["summary"]) == ["1 silent restatement"]
    assert vd.new_parts(res["summary"]) == ["1 endorsement"]
