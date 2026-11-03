"""
Change period, Phase B — the rules in period_change.py. No database.
D3 (who may correct which date), D4 (large), B2 (typed code tied to the
preview), B4 (when an undo is safe), and the switch.
"""
import datetime as dt

import pytest

import period_change as pc

D = dt.date
IN, EX = D(2026, 1, 1), D(2026, 12, 31)


# ── the switch ─────────────────────────────────────────────────────────────

def test_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv(pc.SWITCH, raising=False)
    assert pc.enabled() is False


@pytest.mark.parametrize("v,on", [("true", True), ("1", True), ("on", True),
                                  ("false", False), ("0", False), ("", False)])
def test_switch_values(monkeypatch, v, on):
    monkeypatch.setenv(pc.SWITCH, v)
    assert pc.enabled() is on


# ── D3 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("state,inc,exp", [
    ("active", True, True), ("expired", True, True),
    ("superseded", False, True), ("terminated", False, True),
    ("draft", False, False), ("pending", False, False), ("in_review", False, False),
    ("agreed", False, False), ("signed", False, False), ("changes_requested", False, False),
])
def test_d3_matrix(state, inc, exp):
    assert pc.allowed(state) == {"inception_dt": inc, "expiry_dt": exp}


def test_draft_is_sent_to_edit_terms():
    e = pc.field_errors("draft", IN, EX, IN, D(2026, 11, 30))
    assert "Edit terms" in e["_"]


def test_superseded_start_refused_end_allowed():
    assert "inception_dt" in pc.field_errors("superseded", IN, EX, D(2026, 2, 1), EX)
    assert pc.field_errors("superseded", IN, EX, IN, D(2026, 11, 30)) == {}


def test_terminated_end_never_before_termination():
    term = D(2026, 6, 30)
    e = pc.field_errors("terminated", IN, EX, IN, D(2026, 6, 29), terminated_date=term)
    assert "termination date" in e["expiry_dt"]
    assert pc.field_errors("terminated", IN, EX, IN, term, terminated_date=term) == {}
    assert pc.field_errors("terminated", IN, EX, IN, D(2026, 9, 30), terminated_date=term) == {}


def test_terminated_start_refused():
    e = pc.field_errors("terminated", IN, EX, D(2026, 1, 2), EX, terminated_date=D(2026, 6, 30))
    assert "inception_dt" in e


def test_nothing_changed_and_reversed_dates():
    assert "_" in pc.field_errors("active", IN, EX, IN, EX)
    assert "expiry_dt" in pc.field_errors("active", IN, EX, EX, IN)
    assert "expiry_dt" in pc.field_errors("active", IN, EX, IN, None)


# ── D4 ─────────────────────────────────────────────────────────────────────

def test_31_days_is_not_large_32_is(monkeypatch):
    monkeypatch.delenv(pc.LARGE_ENV, raising=False)
    assert pc.large(IN, EX, IN, EX + dt.timedelta(days=31))["large"] is False
    big = pc.large(IN, EX, IN, EX + dt.timedelta(days=32))
    assert big["large"] is True and big["expiry_days"] == 32 and "32 days" in big["reasons"][0]


def test_either_date_counts_and_direction_does_not_matter():
    assert pc.large(IN, EX, IN - dt.timedelta(days=40), EX, limit=31)["large"] is True


@pytest.mark.parametrize("v,n", [("10", 10), ("0", 0), ("-5", 31), ("x", 31), ("", 31)])
def test_threshold_setting(monkeypatch, v, n):
    monkeypatch.setenv(pc.LARGE_ENV, v)
    assert pc.large_days() == n


# ── B2 ─────────────────────────────────────────────────────────────────────

def test_code_must_match_exactly():
    assert pc.code_matches(" CTR-A8X5X4 ", "CTR-A8X5X4")
    assert not pc.code_matches("ctr-a8x5x4", "CTR-A8X5X4")
    assert not pc.code_matches("CTRA8X5X4", "CTR-A8X5X4")
    assert not pc.code_matches("", "CTR-A8X5X4")


def test_token_changes_with_dates_and_state():
    t = pc.preview_token(7, IN, EX, ["a"])
    assert t == pc.preview_token(7, IN, EX, ["a"])
    assert t != pc.preview_token(7, IN, D(2026, 12, 30), ["a"])
    assert t != pc.preview_token(7, IN, EX, ["b"])
    assert t != pc.preview_token(8, IN, EX, ["a"])


# ── B4: undo ───────────────────────────────────────────────────────────────

def change(**kw):
    base = {"ref": "r1", "new": ["2026-01-01", "2026-11-30"], "old": ["2026-01-01", "2026-12-31"],
            "lifecycle_after": "active",
            "neighbours": {"prev": [5, "2025-01-01", "2025-12-31"], "next": None},
            "rules_after": {"11": "fpA", "12": "fpB"},
            "marks": {k: f"{k}-1" for k in pc.MARKS}}
    base.update(kw)
    return base


def now(**kw):
    base = {"latest_ref": "r1", "latest_kind": "correction",
            "dates": ["2026-01-01", "2026-11-30"], "lifecycle": "active",
            "neighbours": {"prev": [5, "2025-01-01", "2025-12-31"], "next": None},
            "rules": {11: "fpA", 12: "fpB"},
            "marks": {k: f"{k}-1" for k in pc.MARKS},
            "conflicts": []}
    base.update(kw)
    return base


def codes(b):
    return [x["code"] for x in b]


def test_undo_allowed_when_nothing_changed():
    assert pc.undo_blockers(change(), now()) == []


def test_only_the_latest():
    assert codes(pc.undo_blockers(change(), now(latest_ref="r2"))) == ["not_latest"]


def test_an_undo_cannot_be_undone():
    b = pc.undo_blockers(change(), now(latest_ref="u1", latest_kind="undo", latest_undoes="r1"))
    assert codes(b) == ["already_undone"]


def test_dates_changed_since():
    assert "dates_changed" in codes(pc.undo_blockers(change(), now(dates=["2026-01-01", "2026-10-31"])))


def test_terminated_or_superseded_since():
    assert codes(pc.undo_blockers(change(), now(lifecycle="terminated"))) == ["state_changed"]


def test_renewal_created_since():
    b = pc.undo_blockers(change(), now(neighbours={"prev": [5, "2025-01-01", "2025-12-31"],
                                                   "next": [9, "2026-12-01", "2027-11-30"]}))
    assert codes(b) == ["renewal_changed"]


def test_neighbour_dates_changed_since():
    b = pc.undo_blockers(change(), now(neighbours={"prev": [5, "2025-01-01", "2025-12-30"],
                                                   "next": None}))
    assert codes(b) == ["neighbours_changed"]


def test_rule_edited_or_removed_since():
    b = pc.undo_blockers(change(), now(rules={11: "fpX", 12: None}))
    assert codes(b) == ["rules_changed"] and "11, 12" in b[0]["message"]


@pytest.mark.parametrize("key", list(pc.MARKS))
def test_any_file_activity_since_blocks(key):
    m = now()["marks"] | {key: now()["marks"][key] + "-changed"}
    b = pc.undo_blockers(change(), now(marks=m))
    assert codes(b) == ["files_since"] and pc.MARKS[key] in b[0]["message"]


def test_old_dates_would_overlap_now():
    b = pc.undo_blockers(change(), now(conflicts=[{"message": "both would claim 3 days"}]))
    assert codes(b) == ["overlap"]


def test_every_reason_is_reported():
    b = pc.undo_blockers(change(), now(latest_ref="r2", lifecycle="terminated",
                                       rules={11: None, 12: "fpB"},
                                       marks=now()["marks"] | {"arrivals": "a2"}))
    assert codes(b) == ["not_latest", "state_changed", "rules_changed", "files_since"]


def test_rule_fingerprint_is_stable_and_sensitive():
    a = pc.rule_fingerprint({"x": "2026-12-31"}, "n", "m", "d")
    assert a == pc.rule_fingerprint({"x": "2026-12-31"}, "n", "m", "d")
    assert a != pc.rule_fingerprint({"x": "2026-11-30"}, "n", "m", "d")


def test_a_removed_record_also_counts_as_activity():
    m = now()["marks"] | {"exports": None}
    assert codes(pc.undo_blockers(change(), now(marks=m))) == ["files_since"]


# ── the broker email (D13) and the review list ─────────────────────────────

def test_broker_email_carries_the_reason_and_cannot_carry_a_note():
    import inspect
    from email_utils import period_change_email_html
    params = set(inspect.signature(period_change_email_html).parameters)
    assert not {"note", "internal_note", "evidence"} & params
    html = period_change_email_html("Wani", "Mahi Corp", "Demo prog", "Contract X", "CTR-ABC123",
                                    "01 Jul 2026 to 30 Jun 2027", "01 Jul 2026 to 31 Mar 2027",
                                    "Signed schedule <says> 31 Mar")
    assert "Signed schedule &lt;says&gt; 31 Mar" in html and "CTR-ABC123" in html
    assert "31 Mar 2027" in html and "corrected the dates" in html
    undone = period_change_email_html(None, "C", "P", "N", "K", "a", "b", "r", undone=True)
    assert "undone" in undone.lower()


def test_review_items_only_outside_the_term():
    import contract_routes as cr
    files = [{"kind": "file", "id": 1, "label": "jul", "period": "2026-07", "start": None, "end": None},
             {"kind": "file", "id": 2, "label": "sep", "period": "2026-09", "start": None, "end": None},
             {"kind": "result", "id": 3, "label": "q", "period": None,
              "start": "2027-03-01", "end": "2027-05-31"},
             {"kind": "result", "id": 4, "label": "none", "period": None, "start": None, "end": None}]
    items = cr._review_items(files, D(2026, 8, 1), D(2027, 3, 31))
    assert [(i["id"], i["why"]) for i in items] == [
        (1, "outside the corrected term"), (3, "partly outside the corrected term")]


def test_an_undo_as_latest_refuses_with_that_reason_alone():
    b = pc.undo_blockers(change(), now(latest_ref="u1", latest_kind="undo", latest_undoes="r1",
                                       dates=["2026-01-01", "2026-12-31"], lifecycle="terminated"))
    assert codes(b) == ["already_undone"]


# ── Kavachio's evidence route (B1) and the Audit Logs line ─────────────────

def _evidence_world(over=None):
    """A fake session holding one large correction, its contract and its
    evidence document — each test breaks one link of that chain."""
    over = dict(over or {})
    from types import SimpleNamespace as NS
    import contract_routes as cr
    from db import ActivityEvent
    details = {"contract_id": 7, "evidence_document_id": 70, "evidence_filename": "sched.pdf",
               "large": {"large": True}}
    details.update(over.pop("details", {}))
    rows = {
        (ActivityEvent, 5): NS(id=5, action=pc.ACTION_CORRECTED, target="contract:7",
                               tenant_id=3, details=details),
        (cr.Contract, 7): NS(id=7, tenant_id=3),
        (cr.ContractDocument, 70): NS(id=70, contract_id=7, kind="evidence",
                                      filename="sched.pdf", blob_ref="b", blob=None),
    }
    for key, patch in over.items():
        model, rid = key
        rows[(model, rid)] = None if patch is None else NS(**{**vars(rows[(model, rid)]), **patch})
    return NS(get=lambda model, rid: rows.get((model, rid)))


def test_kavachio_evidence_only_through_a_large_corrections_row():
    import contract_routes as cr
    from db import ActivityEvent
    assert cr._correction_evidence(_evidence_world(), 5).id == 70
    broken = [
        {(ActivityEvent, 5): None},                                   # no such row
        {(ActivityEvent, 5): {"action": pc.ACTION_UNDONE}},           # an undo row
        {(ActivityEvent, 5): {"action": "contract_uploaded"}},        # any other row
        {"details": {"large": {"large": False}}},                     # not large
        {"details": {"evidence_document_id": None}},                  # no evidence
        {(ActivityEvent, 5): {"target": "contract:8"}},               # row names another contract
        {(cr.Contract, 7): {"tenant_id": 4}},                         # another carrier's row
        {(cr.ContractDocument, 70): {"contract_id": 8}},              # another contract's file
        {(cr.ContractDocument, 70): {"kind": "contract"}},            # not an evidence file
        {(cr.ContractDocument, 70): None},
    ]
    for over in broken:
        assert cr._correction_evidence(_evidence_world(over), 5) is None, over


def test_kavachio_evidence_route_refuses_everyone_else(monkeypatch):
    from types import SimpleNamespace as NS
    from fastapi import HTTPException
    import contract_routes as cr

    class _S:
        def __init__(self, world):
            self.world = world
        def __enter__(self):
            return self.world
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cr, "SessionLocal", lambda: _S(_evidence_world()))
    monkeypatch.setattr(cr.storage, "resolve_bytes", lambda ref, blob: b"%PDF-evidence")
    for who in ("carrier_admin", "broker_admin"):
        with pytest.raises(HTTPException) as e:
            cr.period_evidence_for_kavachio(5, p=NS(is_platform_admin=False, role=who))
        assert e.value.status_code == 403
    r = cr.period_evidence_for_kavachio(5, p=NS(is_platform_admin=True, role="kavachio_admin"))
    assert r.body == b"%PDF-evidence" and "sched.pdf" in r.headers["content-disposition"]
    with pytest.raises(HTTPException) as e:
        cr.period_evidence_for_kavachio(6, p=NS(is_platform_admin=True, role="kavachio_admin"))
    assert e.value.status_code == 404
    # Kavachio still never opens the contract record itself.
    from carrier_scope import assert_can_open_contract
    with pytest.raises(HTTPException):
        assert_can_open_contract(NS(is_platform_admin=True))


def test_audit_row_evidence_button_is_kavachios_alone():
    from types import SimpleNamespace as NS
    import audit_feed as af
    raw = {"kind": "activity", "id": 5, "action": pc.ACTION_CORRECTED,
           "details": {"evidence_document_id": 70, "evidence_filename": "sched.pdf"}}
    assert af._evidence_link(raw, NS(is_platform=True)) == {"event_id": 5, "filename": "sched.pdf"}
    assert af._evidence_link(raw, NS(is_platform=False)) is None          # carrier or broker
    assert af._evidence_link({**raw, "action": pc.ACTION_UNDONE}, NS(is_platform=True)) is None
    assert af._evidence_link({**raw, "details": {}}, NS(is_platform=True)) is None


def test_audit_line_for_a_correction_and_its_undo():
    import audit_names as an
    d = {"change_ref": "abc", "kind": "correction", "contract_id": 7,
         "old": {"inception_dt": "2026-07-01", "expiry_dt": "2027-06-30"},
         "new": {"inception_dt": "2026-07-01", "expiry_dt": "2027-03-31"},
         "large": {"large": True}, "reason": "Signed schedule", "internal_note": "seen by Mahi",
         "evidence_filename": "sched.pdf"}
    words = [t for t, _ in an._period_change(None, d)]
    assert words == ["End 30 Jun 2027 → 31 Mar 2027", "Large correction", "Reason: Signed schedule",
                     "Internal note: seen by Mahi", "Evidence: sched.pdf"]
    assert not any("Change ref" in w or "Kind" in w for w in words)
    u = {"kind": "undo", "old": d["new"], "new": d["old"], "large": {"large": False},
         "reverses_large": True, "reason": "Wrong page"}
    assert [t for t, _ in an._period_change(None, u)] == [
        "End 31 Mar 2027 → 30 Jun 2027", "Undid a large correction", "Reason: Wrong page"]


# ── fixes from the independent review (16 Oct 2026) ────────────────────────

def _w(code, with_id, msg="m"):
    return {"code": code, "with_id": with_id, "days": 3, "message": msg, "blocking": True}


def test_an_existing_overlap_on_an_untouched_side_does_not_block(monkeypatch):
    import contract_routes as cr
    import renewal_chain as rc
    monkeypatch.setattr(rc, "blocking", lambda conf: [w for w in conf if w.get("blocking")])
    start_overlap = _w("overlaps_previous", 5)
    # Only the end moves: the start's overlap was already there → kept, not new.
    new, kept = cr._new_blocking([start_overlap], [start_overlap], True, False)
    assert new == [] and kept == [start_overlap]
    # The start moves too: the same overlap is now this change's own.
    new, kept = cr._new_blocking([start_overlap], [start_overlap], False, False)
    assert new == [start_overlap] and kept == []
    # A new overlap at the end blocks even though the start's was already there.
    end_overlap = _w("overlaps_next", 9)
    new, _ = cr._new_blocking([start_overlap, end_overlap], [start_overlap], True, False)
    assert new == [end_overlap]
    # Never waived: a contract with no days of its own.
    nodays = _w("no_days", 5)
    new, _ = cr._new_blocking([nodays], [nodays], True, True)
    assert new == [nodays]


def test_a_date_that_is_not_a_date_is_a_400_not_a_500():
    from types import SimpleNamespace as NS
    from fastapi import HTTPException
    import contract_routes as cr
    with pytest.raises(HTTPException) as e:
        cr._period_dates(NS(inception_dt="2026-13-45", expiry_dt="2027-01-01"))
    assert e.value.status_code == 400 and e.value.detail["errors"] == {"inception_dt": "not a date"}
    assert cr._period_dates(NS(inception_dt="2026-02-01", expiry_dt=None)) == (D(2026, 2, 1), None)


def test_a_month_covers_the_whole_month_in_the_review_list():
    import contract_routes as cr
    files = [{"kind": "file", "id": 1, "label": "mar", "period": "2027-03", "start": None, "end": None}]
    items = cr._review_items(files, D(2026, 4, 1), D(2027, 3, 15))
    assert [(i["id"], i["why"], i["end"]) for i in items] == [(1, "partly outside the corrected term",
                                                               "2027-03-31")]


def test_rule_other_dates_leave_out_the_contracts_own_dates(monkeypatch):
    from types import SimpleNamespace as NS
    import contract_routes as cr
    c = NS(id=7, inception_dt=D(2026, 7, 1), expiry_dt=D(2027, 6, 30))
    monkeypatch.setattr(cr, "_period_rule_rows", lambda s, cid, ids=None: [
        (1, {"min": "2026-07-01"}, "Starts 2026-07-01", None, None),          # unchanged start
        (2, {"cut": "2026-11-05"}, "Cut-off", None, None),                    # a genuine other date
        (3, {"max": "2027-06-30"}, "Ends", None, None)])                      # the end, moved
    moves = {D(2027, 6, 30): D(2027, 3, 31)}
    out = cr._period_rules(None, c, moves)
    assert out["other_dates"] == [{"rule_id": 2, "name": "Cut-off", "dates": ["2026-11-05"]}]
    assert [r["rule_id"] for r in out["redated"]] == [3]


def test_a_gap_confirmed_in_a_correction_is_remembered():
    from types import SimpleNamespace as NS
    import contract_routes as cr
    rows = [("contract:7", {"gaps_confirmed": [{"code": "gap_before_next", "days": 31,
                                                "with_id": 8, "message": "m"}]})]

    class Q:
        def filter(self, *a):
            return self
        def all(self):
            return rows
    s = NS(query=lambda *a: Q())
    renewal = NS(id=8, tenant_id=3)
    assert cr._gap_already_confirmed(s, renewal, {"with_id": 7, "days": 31})
    assert not cr._gap_already_confirmed(s, renewal, {"with_id": 7, "days": 30})


def test_the_undo_email_says_undo():
    import inspect
    import contract_routes as cr
    src = inspect.getsource(cr._period_email_thunks)
    assert "undid a correction to the dates" in src and "undone a correction to" in src
