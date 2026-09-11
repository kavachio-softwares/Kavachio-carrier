"""
test_contract_sample_bordereau.py — do the contract's checks actually catch anything?

THE QUESTION THIS ANSWERS, and it is not the one the other files answer. Every
test around it asks whether the right RULE ROWS are written: the right column,
the right comparison, the right severity. None of them asks whether a bordereau
put in front of those rules produces the exceptions it should — and until this
file existed, the answer was no, for every contract ever bound.

The reason is worth stating plainly, because it is invisible from anywhere else:
`duckdb_validation` runs `rule_spec.compiled_sql` and nothing else. A rule
without one is reported as "unprocessable" and never applied to a row. Every
check written from a contract's terms carried its operator and its operand — and
no query. They were recorded, counted on the record, shown on the screen, and
never once run against a file.

So this test builds a bordereau out of the contract's own terms (one clean row,
one row per check that breaks exactly that check, and a row that breaks several
at once) and puts it through the REAL validation engine. What it asserts is the
exception report: this row, this rule, this severity — and silence on the rows
that comply.
"""
import os

os.environ.setdefault("MAIL_ALLOWED_RECIPIENTS", "nobody@example.invalid")

import pytest
from fastapi.testclient import TestClient

import main
import sample_bordereau as sb
from auth_tokens import mint_access_token
from db import (
    AppUser, ExportTemplate, Party, Program, ProgramBroker, SessionLocal,
    Tenant,
)

client = TestClient(main.app)

SHEET = "Risk BDX"

# A template mapped to the data model the way the bordereau setup leaves one:
# a column per canonical field the contract's limits are checked against.
TEMPLATE_COLUMNS = (
    ("Unique Market Reference (UMR)", "policy_umr"),
    ("Certificate Ref", "policy_certificate_reference"),
    ("Insured Full Name, Last Name or Company Name", "policyholder_legal_name"),
    ("Risk Inception Date", "policy_effective_date"),
    ("Risk Expiry Date", "policy_expiration_date"),
    ("Class of Business", "coverage_code"),
    ("Original Currency", "premium_transaction_original_currency"),
    ("Location of risk - Country", "risk_location_country"),
    ("Commission %", "premium_transaction_commission_percent"),
    ("Sum Insured", "policy_sum_insured_amount"),
)


@pytest.fixture(scope="module")
def world():
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        carrier = Tenant(tenant_name=f"bdx-{sfx}", legal_name="Sample Assurance")
        s.add(carrier); s.commit()
        broker = Party(tenant_id=carrier.id, party_type="broker",
                       legal_name="Sample Brokers", reference=f"bdx-b-{sfx}")
        s.add(broker); s.commit()
        prog = Program(tenant_id=carrier.id, name=f"Sample {sfx}",
                       is_app_managed=True)
        s.add(prog); s.commit()
        s.add(ProgramBroker(tenant_id=carrier.id, program_id=prog.id,
                            broker_party_id=broker.id, status="active"))
        founder = (s.query(AppUser).filter(AppUser.role == "kavachio_admin")
                   .order_by(AppUser.id).first())
        if founder is None:
            pytest.skip("no kavachio_admin on this database to seed an invite chain")
        admin = AppUser(tenant_id=carrier.id, email=f"sa-{sfx}@sample.test",
                        full_name="Ada Sample", role="carrier_admin",
                        invited_by_user_id=founder.id)
        s.add(admin); s.commit()

        from ingester import _ensure_carrier_party
        carrier_party_id = _ensure_carrier_party(s, carrier.id)
        s.commit()

        tmpl = ExportTemplate(
            tenant_id=carrier.id, name=f"Sample BDX {sfx}", version=1,
            is_active=1, program_id=prog.id, source_kind="uploaded",
            carrier_party_id=carrier_party_id,
            structure={"sheets": [{
                "sheet_name": SHEET, "header_row": 0, "data_start_row": 1,
                "columns": [{"column_index": i, "column_name": n,
                             "canonical_field": c, "samples": []}
                            for i, (n, c) in enumerate(TEMPLATE_COLUMNS)],
            }]})
        s.add(tmpl); s.commit()
        return {
            "program": prog.id, "broker": broker.id, "template": tmpl.id,
            "carrier": {"Authorization": f"Bearer "
                        f"{mint_access_token(admin.id, carrier.id, 'carrier_admin')}"},
        }


@pytest.fixture(scope="module")
def contract(world):
    """One contract exercising all four shapes of check a limit can become:
    a permitted set, an exclusion, an exact figure and a cap."""
    r = client.post("/contracts", headers=world["carrier"], json={
        "program_id": world["program"], "contract_type": "insurer_broker",
        "name": "Sample terms", "counterparty_party_id": world["broker"],
        "inception_dt": "2026-01-01", "expiry_dt": "2026-12-31",
        "class_of_business": "Property",
        "agreed_limits": {
            "coverage": {"value": "Property Damage"},       # in
            "excluded_territory": {"value": "Puerto Rico"},  # not_in
            "currency": {"value": "USD"},                    # eq, text
            "commission_pct": {"value": 17},                 # eq, numeric
            "max_sum_insured": {"value": 100000},            # lte
        }})
    assert r.status_code in (200, 201), r.text
    rec = r.json()
    # Creating a contract no longer binds checks — authoring writes clauses, and
    # rules are bound at bordereau setup, once the template's columns are known.
    # These tests are about what the checks DO, so bind them the way setup does.
    b = client.post(f"/contracts/{rec['id']}/bind-checks", headers=world["carrier"])
    assert b.status_code == 200, b.text
    return rec


def _validate(rules, rows):
    """The rows through the real engine, exactly as a run does it."""
    import duckdb_validation as dv

    return dv.run_validation(
        [{"sheet": SHEET, "records": [r["values"] for r in rows]}],
        rules)


def test_every_check_carries_a_query_it_can_actually_be_run_with(contract):
    """The bug this file was written for. A rule without `compiled_sql` is not a
    check — the engine reports it as unprocessable and moves on, so the file
    passes and nothing was measured."""
    rules = sb.contract_rules_for(contract["id"])
    assert rules, "the contract bound no checks at all"
    missing = [r["rule_name"] for r in rules
               if not r["rule_spec"].get("compiled_sql")]
    assert not missing, f"these checks cannot be run: {missing}"


def test_the_sample_puts_one_row_in_front_of_every_check(contract):
    """The sample is only worth running if it exercises everything. Derived
    from the rules, so a limit added tomorrow gets a row without anybody
    remembering to write one."""
    rules = sb.contract_rules_for(contract["id"])
    _cols, rows = sb.build_rows(rules)
    broken = {b["rule_id"] for r in rows for b in r["breaks"]}
    assert broken == {r["rule_id"] for r in rules}
    assert any(not r["breaks"] for r in rows), "no row complies with the terms"


def test_a_clean_row_is_left_alone(contract):
    """The half everybody forgets. A check that flags everything catches every
    breach and is worthless — and it looks identical to a good one until a row
    that complies is put in front of it."""
    rules = sb.contract_rules_for(contract["id"])
    _cols, rows = sb.build_rows(rules)
    clean = [r for r in rows if not r["breaks"]]
    out = _validate(rules, clean)
    assert out["unprocessable"] == []
    assert out["exceptions"] == [], out["exceptions"]


def test_each_row_is_caught_by_the_checks_it_breaks_and_no_others(contract):
    """The whole point. Row by row, the exceptions the engine reports are the
    ones worked out independently from the terms — not the ones it happened to
    produce.
    """
    rules = sb.contract_rules_for(contract["id"])
    _cols, rows = sb.build_rows(rules)
    out = _validate(rules, rows)
    assert out["unprocessable"] == [], out["unprocessable"]

    # The engine numbers rows from 1 within the sheet it was handed, which is
    # the number a person reading the report sees beside the row.
    got: dict[int, set] = {}
    for e in out["exceptions"]:
        got.setdefault(int(e["row"]) - 1, set()).add(e.get("rule_id"))

    for i, row in enumerate(rows):
        want = {b["rule_id"] for b in row["breaks"]}
        assert got.get(i, set()) == want, (
            f"row {i} ({row['note']}): expected {want}, got {got.get(i, set())}")


def test_a_breach_is_reported_with_the_severity_the_carrier_chose(contract):
    """An exclusion is critical and a commission variance is a query. If every
    exception came back the same colour the report would need reading in full
    to find the one that stops a settlement."""
    rules = sb.contract_rules_for(contract["id"])
    by_id = {r["rule_id"]: r for r in rules}
    _cols, rows = sb.build_rows(rules)
    out = _validate(rules, rows)
    assert out["exceptions"]
    for e in out["exceptions"]:
        rule = by_id[e["rule_id"]]
        assert e.get("severity") == rule["severity"], e


def test_the_checks_still_run_when_the_bordereau_sheet_is_named_something_else(
        contract):
    """A term does not name a tab. "Commission is 17%" is true of the contract
    whatever the workbook calls its sheet — but the compiled query has to name a
    table, and the one it names is whatever the output template happened to be
    called on the day the checks were written. A carrier who later reports on a
    different jurisdiction's layout, or a new template version, would have every
    term go quiet with "could not be validated", though nothing in the contract
    moved.

    So the sheet is treated as a cache: when NONE of the sheets a query names are
    in the file, the rule is re-targeted from its IR onto the ones that are.
    """
    import duckdb_validation as dv

    rules = sb.contract_rules_for(contract["id"])
    _cols, rows = sb.build_rows(rules)
    expected = _validate(rules, rows)
    assert expected["exceptions"], "nothing to compare against"

    other = "Risk Register 2027"          # the same columns under another name
    out = dv.run_validation(
        [{"sheet": other, "records": [r["values"] for r in rows]}], rules)

    assert out["unprocessable"] == [], out["unprocessable"]
    # Same rows caught by the same rules — only the tab is different.
    def _seen(res):
        return sorted((int(e["row"]), e["rule_id"]) for e in res["exceptions"])
    assert _seen(out) == _seen(expected)


def test_a_rule_is_not_moved_onto_a_schedule_it_was_never_agreed_against(
        contract):
    """The limit of the above, and the reason it is all-or-nothing. A rule that
    still finds one of its own sheets is looking at a file that is missing a
    schedule, not at a renamed one — re-pointing it would measure a schedule the
    contract never scoped it to."""
    import duckdb_validation as dv

    rules = sb.contract_rules_for(contract["id"])
    _cols, rows = sb.build_rows(rules)
    bad = [r for r in rows if r["breaks"]]

    out = dv.run_validation([
        {"sheet": SHEET, "records": []},                       # its own, empty
        {"sheet": "Another Schedule",
         "records": [r["values"] for r in bad]},               # not its own
    ], rules)
    assert out["exceptions"] == [], (
        "a rule whose own sheet is present must stay on it")
