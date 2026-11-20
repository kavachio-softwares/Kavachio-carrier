"""45.2 — the Column Guide is read from each download's own columns.

Run with:  DATABASE_URL=sqlite:// .venv/bin/python -m pytest test_export_glossary.py

Nothing here opens the real database: the Operational Report runs on an
in-memory SQLite session (test_report_delivery._mem_session), the other
reports on hand-made data.
"""
from __future__ import annotations

import datetime as dt
import io

import pytest
from openpyxl import load_workbook

import export_glossary as gl

NOW = dt.datetime(2026, 11, 4, 6, 0)


# ── each download, built the way the app builds it ──────────────────────────
def _operational(cadence="month", period="2026-10", drop=()):
    import report_delivery as rd
    from test_report_delivery import _mem_session
    pack = rd.build_pack(_mem_session(), 1, period, now=NOW, cadence=cadence)
    for k in drop:
        pack.pop(k, None)
    return pack


def _executive(full=True, cadence="month", period="2026-10"):
    import test_executive_delivery as ted
    return ted._full_pack() if full else ted._pack(cadence, period)


def _partner_rows():
    return [dict(rank=1, broker="CRC", programme="Fleet", contract="Fleet 2026", due=3, received=3, on_time=2,
                 on_time_pct=66.7, late=1, late_pct=33.3, missing=0, clean_first_time=1, clean_first_time_pct=50.0,
                 clean_on_time=1, scored=3,
                 clean_on_time_pct=33.3, not_checked=0, files_with_problems=1, problems_open=2, problems_found=5,
                 problems_fixed=3, fixed_pct=60.0, clean_now=2, clean_now_pct=66.7,
                 file_types=["Email"])]


def _policies(seat="carrier"):
    p = dict(policy="P-1", insured="Acme Ltd", programme="Fleet", broker="CRC", insurer="Mahi Corp", state="TX",
             start="2026-01-01", end="2026-12-31", transaction="New", premium=1200.5, currency="USD",
             last_period="2026-10", first_period="2026-01", files=10, rows=12)
    return {"seat": seat, "items": [p], "total": 1, "files": 10}


def _audit_rows():
    return [{"at": "2026-11-04T10:00:00Z", "actor": "A Person", "actor_role": "Carrier", "actor_org": "Mahi Corp",
             "carrier": "Mahi Corp", "action_label": "Uploaded a file", "detail": "Oct file", "bulk": "",
             "target_kind": "Received file", "target": "oct.xlsx", "status": "Done", "category": "activity",
             "ip": "10.0.0.1", "action": "file_uploaded"}]


def _xlsx_of(name, **kw) -> bytes:
    if name == "operational":
        import report_delivery as rd
        return rd.render_xlsx(_operational(**kw), "Test Carrier")
    if name == "executive":
        import executive_delivery as ed
        return ed.render_xlsx(_executive(**kw), "Test Carrier")
    if name == "partner":
        import partner_performance as pp
        return pp.workbook({"rows": _partner_rows()})
    if name == "policies":
        import policy_export as pe
        return pe.workbook(_policies(**kw), {}, org="Mahi Corp", by="A Person", at=NOW)
    if name == "audit":
        import audit_routes as ar
        return ar._xlsx(_audit_rows()).body
    raise KeyError(name)


def _guide(wb) -> dict[str, str]:
    """{column: meaning} as the Column Guide sheet shows them."""
    sh = wb[gl.TITLE]
    assert sh["A1"].value == gl.INTRO
    assert [c.value for c in sh[2]] == gl.HEADER
    return {r[0].value: r[1].value for r in sh.iter_rows(min_row=3) if r[0].value}


def _pdf_story(name, **kw):
    """The PDF's own story, captured just before it is built, so the test can
    read the very tables the file has."""
    import reportlab.platypus as rp
    seen = {}
    real = rp.SimpleDocTemplate.build

    def spy(self, story, *a, **k):
        seen["story"] = list(story)
        return real(self, story, *a, **k)
    rp.SimpleDocTemplate.build = spy
    try:
        if name == "operational":
            import report_delivery as rd
            data = rd.render_pdf(_operational(**kw), "Test Carrier")
        else:
            import executive_delivery as ed
            data = ed.render_pdf(_executive(**kw), "Test Carrier")
    finally:
        rp.SimpleDocTemplate.build = real
    assert data[:4] == b"%PDF"
    return seen["story"]


def _flat(story):
    out = []
    for f in story:
        out.extend(_flat(f._content) if hasattr(f, "_content") else [f])
    return out


def _guide_at(flat):
    return next(i for i, f in enumerate(flat) if getattr(getattr(f, "style", None), "name", "") == "h2"
                and f.getPlainText() == gl.TITLE)


ALL_XLSX = [("operational", {}), ("operational", {"cadence": "week", "period": "2026-10-26"}),
            ("executive", {}), ("executive", {"full": False}), ("partner", {}),
            ("policies", {}), ("policies", {"seat": "broker"}), ("audit", {})]


# ── every heading has a proper meaning ──────────────────────────────────────
@pytest.mark.parametrize("name,kw", ALL_XLSX)
def test_every_excel_heading_has_a_proper_meaning(name, kw):
    wb = load_workbook(io.BytesIO(_xlsx_of(name, **kw)))
    assert gl.unexplained(gl.read_xlsx(wb, skip=["About this list"])) == []


@pytest.mark.parametrize("name,kw", [("operational", {}), ("operational", {"cadence": "week", "period": "2026-10-26"}),
                                     ("executive", {}), ("executive", {"full": False})])
def test_every_pdf_heading_has_a_proper_meaning(name, kw):
    flat = _flat(_pdf_story(name, **kw))
    # The guide is the last table: read only what came before it.
    assert gl.unexplained(gl.read_pdf(flat[:_guide_at(flat)])) == []


# ── the guide matches the file's real columns ───────────────────────────────
@pytest.mark.parametrize("name,kw", ALL_XLSX)
def test_the_excel_guide_lists_exactly_the_columns_the_file_has(name, kw):
    wb = load_workbook(io.BytesIO(_xlsx_of(name, **kw)))
    guide = _guide(wb)
    read = gl.build(gl.read_xlsx(wb, skip=["About this list"]))
    assert list(guide) == [e["heading"] for e in read]
    # Every heading row in the file is covered by one guide line, and the
    # guide names nothing the file does not have.
    covered = {gl.match(h)[0] for h in guide if gl.match(h)}
    for t in gl.read_xlsx(wb, skip=["About this list"]):
        for h, _cells in gl._columns(t):
            if gl.norm(h) in gl._SKIP or gl.is_period(h):
                continue
            assert gl.match(h)[0] in covered, h


def test_the_policy_guide_follows_the_seat():
    carrier = _guide(load_workbook(io.BytesIO(_xlsx_of("policies"))))
    broker = _guide(load_workbook(io.BytesIO(_xlsx_of("policies", seat="broker"))))
    assert "Broker" in carrier and "Carrier" not in carrier
    assert "Carrier" in broker and "Broker" not in broker
    # The list's own record sheet is not a column list.
    assert "Taken out by" not in carrier


@pytest.mark.parametrize("name", ["operational", "executive"])
def test_the_pdf_guide_is_read_from_the_pdf_tables(name):
    flat = _flat(_pdf_story(name))
    at = _guide_at(flat)
    guide_table = next(f for f in flat[at:] if type(f).__name__ == "Table")
    shown = [gl._text(r[0]) for r in guide_table._cellvalues]
    assert shown[0] == "Column"
    assert shown[1:] == [e["heading"] for e in gl.build(gl.read_pdf(flat[:at]))]


# ── optional sections and changing headings ─────────────────────────────────
def test_a_section_left_out_leaves_its_columns_out_of_the_guide():
    full = _guide(load_workbook(io.BytesIO(_xlsx_of("operational"))))
    bare = _guide(load_workbook(io.BytesIO(_xlsx_of("operational", drop=("top_checks", "waiting")))))
    assert "Severity" in full and "Owner" in full
    assert "Severity" not in bare and "Owner" not in bare and "Unresolved for" not in bare
    # Executive: a report sent before the quality sections has no Severity.
    old = _guide(load_workbook(io.BytesIO(_xlsx_of("executive", full=False))))
    new = _guide(load_workbook(io.BytesIO(_xlsx_of("executive"))))
    assert "Severity" not in old and "Severity" in new and "Premium cap" in new


def test_headings_that_change_with_the_period_are_still_matched():
    month = _guide(load_workbook(io.BytesIO(_xlsx_of("operational"))))
    week = _guide(load_workbook(io.BytesIO(_xlsx_of("operational", cadence="week", period="2026-10-26"))))
    assert "Still unresolved at month end" in month
    assert "Still unresolved at the end of the week" in week
    assert month["Still unresolved at month end"] == week["Still unresolved at the end of the week"]
    assert "longer than the target" in month["Unresolved for more than 5 days at month end"]
    # The period columns share one line, whatever they are called.
    assert any("October 2026" in k for k in month) and any("Week of 26 Oct 2026" in k for k in week)
    assert month["vs last month"] == week["vs last week"]
    # The report's own target is stated, not a fixed number.
    assert "(5 days)" in month["Fixed within target (5 days)"]


@pytest.mark.parametrize("heading,same_as", [
    ("Fixed within target (3 days)", "Fixed within target"),
    ("New (rows)", "New policies"),
    ("Gross premium so far", "Gross premium"),
    ("Commission so far", "Commission"),
    ("vs the day before", "vs last year"),
    ("Contract with no files that day", "Contract with no files"),
    ("Waiting longer than target at the end of the day", "Longer than target"),
    ("Average fix time (days)", "Average fix time"),
    ("Timestamp (UTC)", "Timestamp"),
    ("“On time %”", "on time %"),
])
def test_loose_matching(heading, same_as):
    assert gl.match(heading) is not None and gl.match(heading) == gl.match(same_as)


# ── a heading nobody has explained yet ──────────────────────────────────────
@pytest.mark.parametrize("cells,kind,words", [
    ([(0.25, "0%"), (0.5, "0%")], "percentage", "percentage"),
    ([("80%", None)], "percentage", "percentage"),
    ([(dt.date(2026, 1, 1), "d mmm yyyy")], "date", "date"),
    ([("USD 1,200", None)], "amount", "money amount"),
    ([(12, "#,##0"), (3, None)], "number", "as a number"),
    ([("Gold", None)], "text", "as written"),
])
def test_an_unknown_heading_gets_a_plain_meaning_from_its_values(cells, kind, words):
    t = {"where": "Sheet1", "header": ["Loyalty tier score"], "body": [[c] for c in cells], "labels": False}
    e = gl.build([t])[0]
    assert gl.kind_of("Loyalty tier score", cells) == kind
    assert e["generic"] and "Loyalty tier score" in e["meaning"] and words in e["meaning"]
    assert gl.unexplained([t]) == ["Loyalty tier score"]


def test_an_unknown_empty_column_is_read_from_its_name():
    assert gl.kind_of("Refund amount", []) == "amount"
    assert gl.kind_of("Renewal date", []) == "date"
    assert gl.kind_of("Share %", []) == "percentage"


# ── numbers and lists come from the code that uses them ─────────────────────
def test_numbers_and_lists_come_from_the_code_that_uses_them():
    import executive_report as er
    import fix_performance as fp
    import partner_performance as pp
    lim = gl.limits()
    assert lim["near_cap"] == f"{er.NEAR_CAP_PCT:g}"
    for _k, words, _lo, _hi in fp.AGE_BANDS:
        assert words in lim["age_bands"]
    for _k, label in pp.CHANNEL_LABEL.items():
        assert label in lim["channels"] or label in (lim["not_recorded"], lim["not_received"])
    # Every {name} a meaning uses is filled.
    for names, meaning in gl.TERMS:
        assert "{" not in meaning.format(**{**lim, "target": "5 days"}), names


def test_no_meaning_is_stored_twice_under_one_heading():
    seen = {}
    for names, _m in gl.TERMS:
        for n in names:
            assert gl.norm(n) not in seen, f"{n} is listed twice"
            seen[gl.norm(n)] = True


def test_where_it_appears_names_the_section_without_its_live_counts():
    assert gl.section_name("Needs attention — 1 escalation") == "Needs attention"
    assert gl.section_name("Errors still waiting, as at 4 Nov 2026") == "Errors still waiting"
    assert gl.section_name("Key numbers") == "Key numbers"
    flat = _flat(_pdf_story("executive"))
    for e in gl.build(gl.read_pdf(flat[:_guide_at(flat)])):
        assert not any("—" in w or "as at" in w for w in e["where"]), e
