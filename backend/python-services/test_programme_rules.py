"""Programme Rules (programme_rules.py): set by the carrier for a programme,
approved per broker / reinsurer, checking only what each approved."""
import sys
import types

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db
import programme_rules as pr


@pytest.fixture(autouse=True)
def _phase2_on(monkeypatch):
    """These are Phase 2 behaviours, which PHASE2_ENABLED switches off by
    default (phase2.py) — so they are tested with it on."""
    monkeypatch.setenv("PHASE2_ENABLED", "true")

TID, PROG = 7, 1


@pytest.fixture
def s(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(eng, tables=[db.Program.__table__, db.Party.__table__,
                                             db.Contract.__table__, db.TreatyProgramme.__table__])
    pr.RuleBase.metadata.create_all(eng)
    S = sessionmaker(bind=eng)()
    S.add(db.Program(id=PROG, tenant_id=TID, name="Motor"))
    S.add(db.Program(id=2, tenant_id=TID, name="Other"))
    for pid, name in ((50, "Acme"), (51, "Global"), (70, "Swiss Re"), (71, "Hannover")):
        S.add(db.Party(id=pid, legal_name=name, party_type="reinsurer" if pid >= 70 else "broker"))
    S.add(db.Contract(id=10, tenant_id=TID, program_id=PROG, broker_party_id=50,
                      contract_type="insurer_broker", name="Acme binder"))
    S.add(db.Contract(id=11, tenant_id=TID, program_id=PROG, broker_party_id=51,
                      contract_type="insurer_broker", name="Global binder"))
    S.add(db.Contract(id=12, tenant_id=TID, program_id=PROG, broker_party_id=51,
                      contract_type="insurer_broker", name="Old", lifecycle="terminated"))
    S.add(db.Contract(id=20, tenant_id=TID, program_id=PROG, broker_party_id=70,
                      contract_type="insurer_reinsurer", name="QS 2026"))
    S.add(db.Contract(id=30, tenant_id=TID, program_id=2, broker_party_id=50,
                      contract_type="insurer_broker", name="Elsewhere"))
    S.add(db.TreatyProgramme(contract_id=20, program_id=PROG, tenant_id=TID))
    S.commit()
    # The treaty's panel (treaty_routes) — the lead and one more.
    monkeypatch.setattr(pr, "panel", lambda s_, c: {70, 71} if c.id == 20 else set())
    yield S
    S.close()


def body(**k):
    return {"name": "Sum insured cap", "check": "max_sum_insured",
            "operand": "5,000,000", "severity": "critical", "broker_scope": "all",
            "treaty_scope": "none", "approval_from": "broker", **k}


def c(S, cid):
    return S.get(db.Contract, cid)


# ── what a rule may say ─────────────────────────────────────────────────────
def test_a_rule_is_cleaned_into_plain_values(s):
    got = pr.clean(s, TID, PROG, body())
    assert (got["field"], got["operator"], got["operand"]) == ("max_sum_insured", "lte", 5_000_000.0)
    lst = pr.clean(s, TID, PROG, body(check="excluded_territory", operand="Cuba, Iran and Syria"))
    assert (lst["operator"], lst["operand"]) == ("not_in", ["Cuba", "Iran", "Syria"])
    req = pr.clean(s, TID, PROG, body(check="required:policy_number", operand="x"))
    assert (req["operator"], req["operand"]) == ("required", None)
    assert pr.clean(s, TID, PROG, body(check="commission_max_pct", operand="15%"))["operand"] == 15.0
    # No name: called what it checks.
    assert pr.clean(s, TID, PROG, body(name=""))["name"] == "Biggest risk you will cover"


def test_a_rule_saved_before_the_questions_reads_as_one(s):
    old = pr.ProgrammeRule(tenant_id=TID, program_id=PROG, name="Sum insured", field="policy_sum_insured_amount",
                           operator="lte", operand=5_000_000.0, severity="warning", broker_scope="all",
                           treaty_scope="none", approval_from="broker", status="draft", version=1)
    s.add(old)
    s.flush()
    assert pr.check_of(old.field, old.operator) == "max_sum_insured"
    assert pr.sentence(old) == "Biggest risk you will cover: 5,000,000"
    assert pr.view(s, old)["check"] == "max_sum_insured"


def test_rules_read_in_plain_words(s):
    for check, operand, words in (
            ("max_sum_insured", "5000000", "Biggest risk you will cover: 5,000,000"),
            ("commission_max_pct", "15", "Most commission you will pay: 15%"),
            ("excluded_territory", "Cuba, Iran", "Where business may not be written: Cuba, Iran"),
            ("policy_period_months", "12", "Longest policy you will write: 12 months"),
            ("required:insured_name", None, "Insured name must be filled in")):
        assert pr.sentence(pr.create(s, TID, PROG, body(check=check, operand=operand), 5)) == words


@pytest.mark.parametrize("change, words", [
    ({"check": "tenant_code"}, "what the rule should check"),
    ({"operand": "lots"}, "number"),
    ({"operand": ""}, "value"),
    ({"broker_scope": "none"}, "broker contracts, treaties or both"),
    ({"broker_scope": "selected", "broker_contract_ids": [30]}, "this programme"),   # another programme's
    ({"broker_scope": "selected", "broker_contract_ids": [12]}, "this programme"),   # ended
    ({"approval_from": "reinsurer"}, "Reinsurers can only approve"),
    ({"treaty_scope": "all", "check": "min_premium"}, "only be checked on broker contracts"),
])
def test_a_rule_that_cannot_be_saved_says_why(s, change, words):
    with pytest.raises(HTTPException) as e:
        pr.clean(s, TID, PROG, body(**change))
    assert e.value.status_code == 400 and words in e.value.detail


def test_only_the_treaty_rule_kinds_reach_treaties():
    ready = {c["key"] for c in pr.checks() if c["treaty"]}
    assert ready == {"max_sum_insured", "max_tiv", "territory", "excluded_territory", "permitted_risks",
                     "excluded_risks", "currency", "required:policy_number", "required:gross_premium",
                     "required:sum_insured"}
    # Every question is one a contract's terms ask, found the same way.
    from contract_types import AGREED_LIMITS
    assert all(c["key"].startswith("required:") or c["label"] == AGREED_LIMITS[c["key"]]["question"]
               for c in pr.checks())


# ── approval, per party ─────────────────────────────────────────────────────
def test_each_broker_turns_the_rule_on_for_its_own_contracts(s):
    r = pr.create(s, TID, PROG, body(), 5)
    assert not pr.applies_to(s, r, c(s, 10))                     # a draft checks nothing
    asked = pr.send(s, r)
    assert asked == [("broker", 50), ("broker", 51)]              # not the ended contract's twice
    assert pr.view(s, r)["status_line"] == "Waiting for approval (0 of 2)"
    pr.answer(s, r, "broker", 50, pr.APPROVED, user_id=9, name="Acme")
    assert pr.applies_to(s, r, c(s, 10)) and not pr.applies_to(s, r, c(s, 11))
    assert pr.view(s, r)["status_line"] == "Waiting for approval (1 of 2)"
    pr.answer(s, r, "broker", 51, pr.CHANGES, note="Make it 6m")
    assert pr.view(s, r)["status_line"] == "Changes requested by Global"
    assert not pr.applies_to(s, r, c(s, 30))                     # another programme


def test_selected_contracts_only(s):
    r = pr.create(s, TID, PROG, body(broker_scope="selected", broker_contract_ids=[11]), 5)
    assert pr.send(s, r) == [("broker", 51)]
    pr.answer(s, r, "broker", 51, pr.APPROVED)
    assert pr.applies_to(s, r, c(s, 11)) and not pr.applies_to(s, r, c(s, 10))


def test_an_edit_asks_everyone_again(s):
    r = pr.create(s, TID, PROG, body(broker_scope="selected", broker_contract_ids=[10]), 5)
    pr.send(s, r)
    pr.answer(s, r, "broker", 50, pr.APPROVED)
    pr.update(s, r, body(broker_scope="selected", broker_contract_ids=[10], operand=6_000_000))
    assert (r.status, r.version) == ("draft", 2) and not pr.applies_to(s, r, c(s, 10))
    assert pr.view(s, r)["status_line"] == "Draft (version 2)"
    pr.send(s, r)
    assert not pr.applies_to(s, r, c(s, 10))                     # version 1's yes does not count
    pr.answer(s, r, "broker", 50, pr.APPROVED)
    assert pr.applies_to(s, r, c(s, 10))
    pr.retire(r)
    assert not pr.applies_to(s, r, c(s, 10)) and pr.view(s, r)["status_line"] == "Retired"


def test_answers_only_from_who_was_asked(s):
    r = pr.create(s, TID, PROG, body(broker_scope="selected", broker_contract_ids=[10]), 5)
    pr.send(s, r)
    with pytest.raises(HTTPException) as e:
        pr.answer(s, r, "broker", 51, pr.APPROVED)               # Broker B cannot answer for A
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        pr.answer(s, r, "broker", 50, pr.CHANGES, note=" ")
    assert e.value.status_code == 400


def test_a_treaty_needs_every_reinsurer_on_its_panel(s):
    r = pr.create(s, TID, PROG, body(broker_scope="none", treaty_scope="all", approval_from="reinsurer"), 5)
    assert pr.send(s, r) == [("reinsurer", 70), ("reinsurer", 71)]
    pr.answer(s, r, "reinsurer", 70, pr.APPROVED, name="Ana")
    assert not pr.applies_to(s, r, c(s, 20)) and pr.for_treaty(s, c(s, 20)) == []
    pr.answer(s, r, "reinsurer", 71, pr.APPROVED, name="Ben")
    got = pr.for_treaty(s, c(s, 20))
    assert len(got) == 1 and (got[0]["kind"], got[0]["subject"], got[0]["params"]) == (
        "max", "sum_insured", {"value": 5_000_000.0})
    assert got[0]["clause"] == "Biggest risk you will cover: 5,000,000"
    assert got[0]["source"] == "programme" and got[0]["severity"] == "critical"


def test_a_side_not_asked_is_checked_once_sent(s):
    r = pr.create(s, TID, PROG, body(treaty_scope="all"), 5)          # brokers approve; treaties not asked
    assert pr.send(s, r) == [("broker", 50), ("broker", 51)]
    assert pr.applies_to(s, r, c(s, 20)) and not pr.applies_to(s, r, c(s, 10))


# ── checking ────────────────────────────────────────────────────────────────
def test_a_broker_file_gets_the_rule_compiled_for_its_own_template(s, monkeypatch):
    import contract_rules as cr
    fields = [{"name": "Sum Insured", "sheet": "Premium", "canonical_field": "policy_sum_insured_amount"}]
    monkeypatch.setattr(cr, "_find_column",
                        lambda wanted, tf: (next((f for f in tf if f["canonical_field"] in wanted), None), []))
    monkeypatch.setitem(sys.modules, "app_routes",
                        types.SimpleNamespace(_template_fields_from_structure=lambda st: fields))
    monkeypatch.setattr(cr, "_compile", lambda ir, field, tf, sheet: f"SQL {ir['template']} {field}")
    r = pr.create(s, TID, PROG, body(), 5)
    pr.send(s, r)
    assert pr.for_broker_run(s, TID, [c(s, 10)], {}) == []        # not approved yet
    pr.answer(s, r, "broker", 50, pr.APPROVED)
    got = pr.for_broker_run(s, TID, [c(s, 10), c(s, 11)], {})
    assert len(got) == 1
    g = got[0]
    assert (g["rule_id"], g["contract_id"], g["no_cache"], g["severity"]) == (-r.id, 10, True, "critical")
    assert g["rule_spec"]["compiled_sql"] == "SQL max_limit Sum Insured"
    assert g["rule_name"] == "Programme rule: Sum insured cap"
    # A template with no such column: kept, with no query — the run reports it.
    monkeypatch.setitem(sys.modules, "app_routes",
                        types.SimpleNamespace(_template_fields_from_structure=lambda st: []))
    got = pr.for_broker_run(s, TID, [c(s, 10)], {})
    assert got[0]["rule_spec"]["compiled_sql"] is None
    assert got[0]["no_column"] == ("This bordereau has no column for “Biggest risk you will cover: "
                                   "5,000,000”, so it could not be checked.")
    import duckdb_validation as dv
    assert dv._compiled_sql_for(got[0]) == (None, got[0]["no_column"])


def test_the_engine_does_not_cache_a_programme_rule():
    import duckdb_validation as dv
    assert dv._save_cache(None, {"rule_id": -3, "no_cache": True}, 1, "a", "b", {}) is None


def test_missing_tables_say_so():
    eng = create_engine("sqlite:///:memory:")
    S = sessionmaker(bind=eng)()
    assert pr.missing_tables(S)
    with pytest.raises(HTTPException) as e:
        pr.require_tables(S)
    assert e.value.status_code == 409 and "migration 48" in e.value.detail
    assert pr.for_broker_run(S, TID, [types.SimpleNamespace(program_id=1)], {}) == []
