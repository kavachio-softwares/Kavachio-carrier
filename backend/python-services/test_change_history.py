"""Tasks 48.1 / 48.2 / 49.1 / 49.3 — the pure parts of change and rule history.

No database: nothing here opens a session, so it is safe to run anywhere.
The end-to-end check (real routes, hooks, a disposable Postgres) is described
in PROJECT_CONTEXT.md §9 and must never be pointed at the shared database.
"""
import datetime as dt
import os

# db.py builds its engine from DATABASE_URL when imported; nothing here uses it.
os.environ.setdefault("DATABASE_URL", "sqlite://")

import audit_record_filter as arf  # noqa: E402
import change_history as ch  # noqa: E402
import rule_history as rh  # noqa: E402


def test_show_reads_like_the_screen():
    assert ch.show(None) is None and ch.show("") is None and ch.show({}) is None
    assert ch.show(True) == "Yes" and ch.show(False) == "No"
    assert ch.show(dt.date(2026, 1, 2)) == "2026-01-02"
    assert ch.show(1500.0) == "1500" and ch.show(12.5) == "12.5"
    assert ch.show("x" * 400).endswith("…") and len(ch.show("x" * 400)) == ch.CLIP


def test_words():
    assert ch.words("inception_dt") == "Inception date"
    assert ch.words("broker_party_id") == "Broker"
    assert ch.words("due_after_days") == "Due after days"


def test_json_changes_keys_lists_by_name_not_position():
    old = {"sections": [{"key": "a", "body": "one"}, {"key": "b", "body": "two"}]}
    new = {"sections": [{"key": "b", "body": "two"}, {"key": "a", "body": "ONE"}]}
    out = ch.json_changes("Wording", old, new)
    assert out == [{"field": "Wording › Sections › A › Body", "old": "one", "new": "ONE"}]


def test_agreed_terms_are_named_by_their_question():
    out = ch.json_changes("Agreed terms", {"max_sum_insured": {"value": 5, "severity": "critical"}},
                          {"max_sum_insured": {"value": 7, "severity": "warning"}}, path_words=ch._limit_words)
    fields = {c["field"] for c in out}
    assert any(f.startswith("Agreed terms › ") and "(how strict)" not in f for f in fields)
    assert any(f.endswith("(how strict)") for f in fields)


def test_reason_header_is_decoded_and_bounded():
    assert ch.clean_reason("Business%20update%20%E2%80%94%20renewal") == "Business update — renewal"
    assert ch.clean_reason("   ") is None and ch.clean_reason(None) is None
    assert len(ch.clean_reason("a" * 900)) == 500


def test_event_words():
    assert ch._event_of([{"field": "Switched on", "old": "Yes", "new": "No"}], False, False) == "switched_off"
    assert ch._event_of([{"field": "Status", "old": "sent", "new": "retired"}], False, False) == "retired"
    assert ch._event_of([{"field": "Name", "old": "a", "new": "b"}], False, False) == "changed"
    assert ch._event_of([], True, False) == "created"


def test_draft_changed_matches_the_same_persons_save_within_a_minute():
    t = dt.datetime(2026, 11, 19, 10, 0, 0)
    edits = [(t, 7, 101), (t + dt.timedelta(minutes=5), 7, 102), (t, 8, 103)]
    assert ch.match_edit(edits, t + dt.timedelta(seconds=1), 7) == 101
    assert ch.match_edit(edits, t + dt.timedelta(minutes=2), 7) is None
    assert ch.match_edit(edits, t, 8) == 103


def _rule(**kw):
    base = {"rule_id": 1, "contract_id": 9, "rule_name": "Minimum Premium", "severity": "critical",
            "rule_status": "active", "error_message": "too low",
            "canonical_target": {"output_field": "Gross Premium"},
            "rule_spec": {"params": {"tolerance_pct": 5}, "compiled_sql": "select 1"}}
    base.update(kw)
    return base


def test_rule_diff_ignores_the_compiled_query():
    a = _rule()
    b = _rule(rule_spec={"params": {"tolerance_pct": 5}, "compiled_sql": "select 2"})
    assert rh.diff(a, b) == []
    c = _rule(rule_spec={"params": {"tolerance_pct": 10}, "compiled_sql": "select 3"})
    assert rh.diff(a, c) == [{"field": "Settings › Params › Tolerance pct", "old": "5", "new": "10"}]


def test_rule_events_and_key():
    assert rh._event(_rule(), _rule(rule_status="disabled")) == "switched_off"
    assert rh._event(_rule(rule_status="disabled"), _rule()) == "switched_on"
    assert rh._event(None, _rule()) == "created" and rh._event(_rule(), None) == "removed"
    assert rh.key_of(_rule()) == "contract_rule:9:Minimum Premium:Gross Premium"


def test_rule_update_statements_are_recognised():
    from sqlalchemy import text

    class Conn:
        info: dict = {}
    sql = text("UPDATE validation_rule SET rule_spec = CAST(:rs AS JSONB) WHERE rule_id = :rid")
    assert rh._match(Conn(), sql, {"rid": 4, "rs": "{}"}) == [4]
    many = text("UPDATE validation_rule SET rule_status = 'disabled' WHERE rule_id = ANY(:ids)")
    assert rh._match(Conn(), many, {"ids": [1, 2]}) == [1, 2]
    other = text("UPDATE validation_rule SET x = 1 WHERE clause_id = :c")
    assert rh._match(Conn(), other, {"c": 1}) is None


def test_old_value_is_kept_when_the_code_never_read_it(monkeypatch):
    """A field changed right after a commit (every attribute expired) keeps its
    real old value, and setting a field to what it already was is no change.
    In-memory SQLite only — nothing outside this test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import audit
    import db
    monkeypatch.setattr(audit, "actor_identity", lambda uid: (None, None, None))
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(eng, tables=[db.Program.__table__, db.ActivityEvent.__table__])
    ch.install()
    tok = ch._ctx.set({"user_id": 7, "tenant_id": 1, "reason": None})
    try:
        with sessionmaker(bind=eng)() as s:
            p = db.Program(tenant_id=1, name="Motor")
            s.add(p)
            s.commit()
            p.name = "Motor 2"            # expired by the commit, never read
            s.commit()
            p.name = "Motor 2"            # the same value again
            s.commit()
            rows = [e.details for e in s.query(db.ActivityEvent).filter_by(action=ch.ACTION)
                    .order_by(db.ActivityEvent.id)]
    finally:
        ch._ctx.reset(tok)
    assert [r["event"] for r in rows] == ["created", "changed"]
    assert rows[1]["changes"] == [{"field": "Name", "old": "Motor", "new": "Motor 2"}]


def test_record_filter_parse():
    assert arf.parse("contract:12") == ("contract", "12")
    assert arf.parse("policy: P-1 ") == ("policy", "P-1")
    assert arf.parse("file:") is None and arf.parse("other:1") is None and arf.parse(None) is None
    assert "/export/downloads/" in arf._file_paths(arf.Record("file", "x", export_ids=[3, 4]))
