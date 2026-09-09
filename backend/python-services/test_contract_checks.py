"""
test_contract_checks.py — the step between a contract and a bordereau.

WHAT WAS MISSING, AND WHY IT WAS INVISIBLE. A carrier writes a contract with
ten limits on it. Every one is stated in the wording, shown on the record and
signed for. Not one of them is checked against anything, because a check is a
comparison against a bordereau COLUMN — until the contract is bound to the
output template it reports into, there are no columns and there are no rules.
Nothing on any screen said so, so a contract with no checks looked exactly like
a contract with ten.

Two things are guarded here:

  · the RECORD REPORTS THE GAP. `checks.rules` beside `checks.checkable` is the
    difference between "everything you agreed is measured" and "none of it is",
    and it is the only thing that makes the second visible.

  · BINDING IS A TRANSLATION, NOT A READING. A contract written here already
    carries each limit with the comparison and the severity the carrier chose,
    so binding is deterministic: same terms, same rules, every time, no model
    call. Bind twice and there is one set of checks, not two that both fire.
"""
import os

os.environ.setdefault("MAIL_ALLOWED_RECIPIENTS", "nobody@example.invalid")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import contract_types as ct
import main
from auth_tokens import mint_access_token
from db import (
    AppUser, ExportTemplate, Party, Program, ProgramBroker, SessionLocal,
    Tenant,
)

client = TestClient(main.app)


def _cols(*pairs):
    return [{"column_index": i, "column_name": name, "canonical_field": canon,
             "samples": []}
            for i, (name, canon) in enumerate(pairs)]


@pytest.fixture(scope="module")
def world():
    """A carrier with a broker, a programme, and a bordereau template to report
    into — which is the thing that makes a check possible at all."""
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        carrier = Tenant(tenant_name=f"chk-{sfx}", legal_name="Checkwright Re")
        s.add(carrier); s.commit()
        broker = Party(tenant_id=carrier.id, party_type="broker",
                       legal_name="Bind & Co", reference=f"chk-b-{sfx}")
        s.add(broker); s.commit()
        prog = Program(tenant_id=carrier.id, name=f"Checks {sfx}",
                       is_app_managed=True)
        s.add(prog); s.commit()
        s.add(ProgramBroker(tenant_id=carrier.id, program_id=prog.id,
                            broker_party_id=broker.id, status="active"))
        founder = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
                   .order_by(AppUser.id).first())
        if founder is None:
            pytest.skip("no kavachio_admin on this database to seed an invite chain")
        admin = AppUser(tenant_id=carrier.id, email=f"cw-{sfx}@checkwright.test",
                        full_name="Ola Bright", role="carrier_admin",
                        invited_by_user_id=founder.id)
        s.add(admin); s.commit()

        # The carrier's own party row. Every template the app creates is
        # scoped to it and the resolve ladder filters on it, so a fixture that
        # left it null would be testing a template no screen can produce.
        from ingester import _ensure_carrier_party
        carrier_party_id = _ensure_carrier_party(s, carrier.id)
        s.commit()

        # A template with a column for the commission and one for the sum
        # insured — and deliberately none for the deductible, so there is
        # something the contract says that this bordereau cannot measure.
        tmpl = ExportTemplate(
            tenant_id=carrier.id, name=f"Monthly BDX {sfx}", version=1,
            is_active=1, program_id=prog.id, source_kind="uploaded",
            carrier_party_id=carrier_party_id,
            structure={"sheets": [{
                "sheet_name": "Risk", "header_row": 0, "data_start_row": 1,
                "columns": _cols(("Commission Rate", "commission_pct"),
                                 ("Sum Insured", "sum_insured")),
            }]})
        s.add(tmpl); s.commit()

        # A second programme with no bordereau template at all, so the
        # "nothing to bind against" path is exercised against a real absence
        # rather than a mock of one.
        bare = Program(tenant_id=carrier.id, name=f"Unbound {sfx}",
                       is_app_managed=True)
        s.add(bare); s.commit()
        s.add(ProgramBroker(tenant_id=carrier.id, program_id=bare.id,
                            broker_party_id=broker.id, status="active"))
        s.commit()

        return {
            "program": prog.id, "bare_program": bare.id,
            "broker": broker.id, "template": tmpl.id,
            "template_name": tmpl.name,
            "carrier": {"Authorization": f"Bearer "
                        f"{mint_access_token(admin.id, carrier.id, 'carrier_admin')}"},
        }


def _raise_contract(world, **limits):
    """A contract written here, with the limits given."""
    body = {
        "program_id": world["program"], "contract_type": "insurer_broker",
        "name": "Bindable", "counterparty_party_id": world["broker"],
        "inception_dt": "2026-01-01", "expiry_dt": "2026-12-31",
        "class_of_business": "Property",
        "agreed_limits": {k: {"value": v} for k, v in limits.items()},
    }
    r = client.post("/contracts", headers=world["carrier"], json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _rule_rows(contract_id: int) -> list[dict]:
    with SessionLocal() as s:
        return [dict(r._mapping) for r in s.execute(text(
            "SELECT rule_name, severity, rule_spec, canonical_target "
            "FROM validation_rule WHERE contract_id = :cid ORDER BY rule_name"),
            {"cid": contract_id})]


def test_a_contract_is_measured_from_the_moment_it_is_raised(world):
    """The step nobody had been shown. A carrier raising a contract with two
    limits on it should not have to find a button before either of them is
    checked — so the checks are written as it is created, against the template
    its programme already reports into."""
    rec = _raise_contract(world, commission_pct=11, max_sum_insured=250000)
    assert rec["checks"]["checkable"] == 2
    assert rec["checks"]["rules"] == 2
    assert rec["mapping"]["rules_written"] == 2


def test_but_a_programme_with_no_bordereau_template_still_takes_a_contract(world):
    """Binding is never at the cost of the thing that was asked for. A
    programme with no template yet is an ordinary state, not a reason to refuse
    a contract — the terms are kept and the button is on the screen."""
    r = client.post("/contracts", headers=world["carrier"], json={
        "program_id": world["bare_program"], "contract_type": "insurer_broker",
        "name": "Unbindable", "counterparty_party_id": world["broker"],
        "inception_dt": "2026-01-01", "expiry_dt": "2026-12-31",
        "class_of_business": "Property",
        "agreed_limits": {"commission_pct": {"value": 11}}})
    assert r.status_code in (200, 201), r.text
    rec = r.json()
    assert rec["checks"] == {"rules": 0, "checkable": 1,
                             "output_template_id": None, "bindable": True,
                             "output_template": None, "sheets": []}
    assert "mapping" not in rec
    # And the button says so rather than pretending.
    out = client.post(f"/contracts/{rec['id']}/bind-checks",
                      headers=world["carrier"])
    assert out.status_code == 409
    assert "no output template" in out.json()["detail"]["message"]


def test_a_term_corrected_after_binding_takes_its_check_with_it(world):
    """The drift this design exists to prevent, in the one place it could still
    happen silently: a contract corrected from 11% to 15% whose checks still
    hold 11%."""
    rec = _raise_contract(world, commission_pct=11)
    assert _rule_rows(rec["id"])[0]["rule_spec"]["operand"] == 11.0
    client.patch(f"/contracts/{rec['id']}", headers=world["carrier"],
                 json={"agreed_limits": {"commission_pct": {"value": "15"}}})
    assert _rule_rows(rec["id"])[0]["rule_spec"]["operand"] == 15.0


def test_binding_turns_the_terms_into_checks(world):
    rec = _raise_contract(world, commission_pct=11, max_sum_insured=250000)
    out = client.post(f"/contracts/{rec['id']}/bind-checks",
                      headers=world["carrier"])
    assert out.status_code == 200, out.text
    m = out.json()["mapping"]
    assert m["rules_written"] == 2
    assert m["output_template"]["name"] == world["template_name"]
    # The record that comes back is the record, not a summary of one.
    assert out.json()["checks"]["rules"] == 2

    # A rule is NAMED with the question the carrier answered, so the exception
    # report says the same words the contract screen did. Read from the
    # vocabulary rather than spelled out here — the wording of a question is
    # contract_types' to change.
    rows = _rule_rows(rec["id"])
    by_limit = {r["rule_spec"]["limit"]: r for r in rows}
    assert set(by_limit) == {"commission_pct", "max_sum_insured"}
    assert (by_limit["commission_pct"]["rule_name"]
            == ct.AGREED_LIMITS["commission_pct"]["question"])
    # The comparison is the one the term already carried, not one inferred here.
    assert by_limit["commission_pct"]["rule_spec"]["operator"] == "eq"
    assert by_limit["max_sum_insured"]["rule_spec"]["operator"] == "lte"
    # And it points at the column the template actually has.
    assert (by_limit["commission_pct"]["canonical_target"]["output_field"]
            == "Commission Rate")


def test_a_term_the_bordereau_cannot_measure_is_named_not_guessed(world):
    """A deductible the template has no column for produces NO rule. Binding it
    to the nearest-looking column would fail rows for the wrong reason, which is
    worse than not checking — it is wrong in a way people act on."""
    rec = _raise_contract(world, commission_pct=11, deductible=5000)
    m = client.post(f"/contracts/{rec['id']}/bind-checks",
                    headers=world["carrier"]).json()["mapping"]
    assert m["rules_written"] == 1
    assert [u["key"] for u in m["unmapped"]] == ["deductible"]
    assert m["unmapped"][0]["question"]
    # Reported as a shortfall on the record too: one of two terms is measured.
    rec2 = client.get(f"/contracts/{rec['id']}", headers=world["carrier"]).json()
    assert rec2["checks"] == {"rules": 1, "checkable": 2,
                              "output_template_id": world["template"],
                              "bindable": True,
                              "output_template": world["template_name"],
                              "sheets": ["Risk"]}


def test_binding_twice_leaves_one_set_of_checks(world):
    """Same contract, same template, twice. Two sets would both fire on every
    row and report the same breach twice."""
    rec = _raise_contract(world, commission_pct=11, max_sum_insured=250000)
    for _ in range(2):
        client.post(f"/contracts/{rec['id']}/bind-checks", headers=world["carrier"])
    assert len(_rule_rows(rec["id"])) == 2


def test_the_button_is_still_there_for_a_template_that_changed(world):
    """Re-binding by hand is what answers the other direction: the terms did not
    move, the TEMPLATE did — a column was mapped, so a term that had nothing to
    measure it now has one."""
    rec = _raise_contract(world, commission_pct=11)
    out = client.post(f"/contracts/{rec['id']}/bind-checks",
                      headers=world["carrier"])
    assert out.status_code == 200
    assert out.json()["checks"]["rules"] == 1


def test_a_contract_with_nothing_checkable_is_refused_rather_than_bound_to_nothing(world):
    """An uploaded wording has no agreed limits — its rules come from reading
    its clauses instead. Silently writing zero rules here would look like a
    successful bind."""
    rec = _raise_contract(world)
    r = client.post(f"/contracts/{rec['id']}/bind-checks", headers=world["carrier"])
    assert r.status_code == 409
    assert "no agreed limits" in r.json()["detail"]["message"]
    assert rec["checks"]["bindable"] is False


def test_the_template_is_resolved_from_the_scope_not_asked_for(world):
    """Nobody picks a template here. Which one applies is already decided by the
    ladder every run uses, and a second answer to that question is how a file
    gets checked against a template nothing reports into."""
    rec = _raise_contract(world, commission_pct=11)
    m = client.post(f"/contracts/{rec['id']}/bind-checks",
                    headers=world["carrier"]).json()["mapping"]
    assert m["output_template"]["id"] == world["template"]
    # Said out loud, so the screen can report "this is the programme's
    # template" rather than implying the carrier chose it.
    assert m["match_level"] in ("contract", "broker", "programme")


# ── which column a term is checked against ──────────────────────────────────
#
# Pure, and the part with the sharpest edge on it. A rule bound to the wrong
# column does not fail loudly — it fails ROWS, for a reason that reads as
# plausible, and somebody queries a broker over it.

import contract_rules as cr
import data_model as dm


def test_every_column_a_limit_looks_for_is_one_the_data_model_has():
    """The bug this test exists for. These names used to be short and
    plausible — `sum_insured`, `risk_country`, `commission_pct` — and the data
    model spells them `policy_sum_insured_amount`, `risk_location_country` and
    `premium_transaction_commission_percent`. Not one of them matched, so every
    limit fell through to matching on a column HEADING and a real bordereau
    checked one term in eight. Nothing said so, because a limit that finds no
    column is reported as unmapped and unmapped looked like the template's
    fault.
    """
    known = set(dm.DATA_MODEL)
    # The one limit the data model has no column for: it holds a policy's two
    # dates, not the span between them. Its candidates are heading matches on
    # purpose — see LIMIT_COLUMNS.
    no_canonical_column = {"policy_period_months"}
    for key, wanted in cr.LIMIT_COLUMNS.items():
        if key in no_canonical_column:
            continue
        assert known & set(wanted), (
            f"{key} looks for {wanted}, none of which the data model has")


def _template(*pairs):
    return [{"name": name, "canonical_field": canon, "sheet": "Risk"}
            for name, canon in pairs]


def test_the_heading_breaks_a_tie_between_columns_claiming_one_meaning():
    """A real Lloyd's template maps five columns to the commission percentage —
    the commission and four tax rates auto-mapped alongside it. Taking whichever
    came last bound the contract's commission to "Tax 5 - %"."""
    fields = _template(
        ("Commission %", "premium_transaction_commission_percent"),
        ("Tax 4 - %", "premium_transaction_commission_percent"),
        ("Tax 5 - %", "premium_transaction_commission_percent"))
    rules, unmapped = cr.map_limits_to_template(
        ct.clean_agreed_limits({"commission_pct": {"value": 13}}), fields)
    assert not unmapped
    assert rules[0]["column"] == "Commission %"


def test_and_when_no_heading_settles_it_nothing_is_chosen():
    """Two columns equally entitled to the check. Guessing between them is the
    thing this module refuses to do — the term is reported instead, with both
    headings named so the fix is a click away in the template."""
    fields = _template(("Rate A", "premium_transaction_commission_percent"),
                       ("Rate B", "premium_transaction_commission_percent"))
    rules, unmapped = cr.map_limits_to_template(
        ct.clean_agreed_limits({"commission_pct": {"value": 13}}), fields)
    assert not rules
    assert unmapped[0]["ambiguous"] == ["Rate A", "Rate B"]
    assert "which one this term is about" in unmapped[0]["reason"]


def test_a_single_mapped_column_is_believed_however_it_is_headed():
    """A template mapped to the data model says what its columns MEAN, and that
    beats anything guessable from a heading. If it says the column headed
    "Number of Instalments" carries the sum insured, the place to correct that
    is the template."""
    fields = _template(("Number of Instalments", "policy_sum_insured_amount"))
    rules, _ = cr.map_limits_to_template(
        ct.clean_agreed_limits({"max_sum_insured": {"value": 100000}}), fields)
    assert rules[0]["column"] == "Number of Instalments"


def test_a_template_nobody_has_mapped_still_matches_on_its_headings():
    """The other half of the ladder, and the reason the short names are kept
    after the canonical ones."""
    fields = _template(("Gross Written Premium", None))
    rules, _ = cr.map_limits_to_template(
        ct.clean_agreed_limits({"min_premium": {"value": 500}}), fields)
    assert rules[0]["column"] == "Gross Written Premium"


def test_the_record_says_which_template_and_sheet_its_checks_are_measured_on(
        world):
    """Nobody is asked which template to bind against — deliberately, because
    two answers to that question is how a file gets checked against a template
    nothing reports into. But the answer was then invisible: a carrier who
    writes no US business could have every term measured on a Lloyd's US layout
    and no screen would say so. The contract does not CHOOSE the template; it
    does have to SHOW it."""
    rec = _raise_contract(world, commission_pct=11)
    client.post(f"/contracts/{rec['id']}/bind-checks", headers=world["carrier"])
    checks = client.get(f"/contracts/{rec['id']}",
                        headers=world["carrier"]).json()["checks"]
    assert checks["output_template"] == world["template_name"]
    assert checks["sheets"] == ["Risk"]


def test_a_check_keeps_the_rule_it_was_written_from_not_just_the_query(world):
    """The compiled query is a CACHE; the IR is the rule. Stored whole, under
    the key every other rule in the table uses, so the engine can recompile a
    term onto the sheets a file actually has instead of reporting it as
    unvalidatable forever (duckdb_validation._refresh_if_stale)."""
    rec = _raise_contract(world, commission_pct=11)
    client.post(f"/contracts/{rec['id']}/bind-checks", headers=world["carrier"])
    spec = _rule_rows(rec["id"])[0]["rule_spec"]
    assert spec["ir"]["template"] == "value_in_set" or spec["ir"]["params"]
    assert spec["ir"]["template"] == spec["template"]


def test_raising_a_contract_creates_no_output_template(world):
    """Asked directly, because the setup screen showing a template beside a
    contract makes it look as though one implies the other.

    Binding RESOLVES a template and never makes one. A template invented at
    contract-creation time would be a second answer to "which template does
    this programme report into", and the runs would use the other one — so a
    contract either finds the programme's template or is measured on nothing
    until somebody builds one on purpose.
    """
    from db import ExportTemplate

    with SessionLocal() as s:
        before = {t.id for t in s.query(ExportTemplate).all()}

    rec = _raise_contract(world, commission_pct=12, max_sum_insured=400000)
    # And not on the explicit path either, which is the one that WRITES rules.
    client.post(f"/contracts/{rec['id']}/bind-checks", headers=world["carrier"])

    with SessionLocal() as s:
        after = {t.id for t in s.query(ExportTemplate).all()}
    assert after == before, f"contract creation made template(s) {after - before}"
    # It found the one the programme already had, which is the whole of its job.
    assert _rule_rows(rec["id"])[0]["rule_spec"]["output_template_id"] == \
        world["template"]


def test_and_a_programme_with_no_template_does_not_get_one_made_for_it(world):
    """The tempting shortcut: no template, so build one. It would be built from
    the contract's own terms — a bordereau shaped like the contract rather than
    like what the broker actually sends — and every run afterwards would report
    into it."""
    from db import ExportTemplate

    with SessionLocal() as s:
        before = {t.id for t in s.query(ExportTemplate).all()}
    r = client.post("/contracts", headers=world["carrier"], json={
        "program_id": world["bare_program"], "contract_type": "insurer_broker",
        "name": "No template here", "counterparty_party_id": world["broker"],
        "inception_dt": "2026-01-01", "expiry_dt": "2026-12-31",
        "class_of_business": "Property",
        "agreed_limits": {"commission_pct": {"value": 12}}})
    assert r.status_code in (200, 201), r.text
    with SessionLocal() as s:
        after = {t.id for t in s.query(ExportTemplate).all()}
    assert after == before
    assert r.json()["checks"]["rules"] == 0
