"""Which contract a bordereau is measured against, chosen at RUN time.

THE GAP. A broker with two live contracts has two different sets of terms, and
which of them a given bordereau was written under is something only the person
holding the file knows. Process Bordereau never asked: the Contract field read
"2 contracts on this broker" and could not be touched, and the run then measured
every file against whichever contract the SETUP happened to be pinned to. The
wrong answer was silent — the file validated, the output generated, and nothing
on the screen said whose terms had just been applied.

Two halves are guarded here, and they fail differently:

  · THE CHOICE REACHES THE VALIDATION. `contract_id` was already a field on the
    run, and it was already used to resolve the output template — but it was
    dropped on the floor the moment a setup was running, which is every real
    run. `_run_contract_for_render` is the decision, and per-schedule pins must
    survive it: a setup that gives each output sheet its own contract is
    answering a different question.

  · THE CHOICE IS CHECKED. It arrives from a form field, and a form field is a
    request, not a fact. Unchecked, a run could be pointed at any contract id
    in the database and somebody else's terms would become the terms this
    bordereau was held to.

    python -m pytest test_run_contract_scope.py
"""
from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

import main  # noqa: F401 — loads .env, which db needs at import
import direct_routes as dr
from db import Contract, Party, Program, ProgramBroker, SessionLocal, Tenant


# ══════════════════════════════════════════════════════════════════════════
#  the decision — no database, no network
# ══════════════════════════════════════════════════════════════════════════
def test_the_contract_named_on_the_run_is_the_one_that_governs():
    """The whole point. A setup pinned to contract 1, a run that says 2, and 2
    is what the file is measured against."""
    assert dr._run_contract_for_render(2, pipeline_id=9, legacy_fallback=1) == 2


def test_naming_nothing_leaves_the_setup_in_charge():
    """Every run made before this picker existed sends no contract, and has to
    behave exactly as it did: the pipeline's own contracts govern, which the
    renderer reads off the pipeline when it is handed None."""
    assert dr._run_contract_for_render(None, pipeline_id=9, legacy_fallback=1) is None


def test_without_a_setup_the_legacy_fallback_still_applies():
    """The no-pipeline path is older than pipelines and still runs. Its fallback
    contract must not be lost to a change about something else."""
    assert dr._run_contract_for_render(None, pipeline_id=None, legacy_fallback=7) == 7
    assert dr._run_contract_for_render(4, pipeline_id=None, legacy_fallback=7) == 4


def test_a_per_schedule_pin_is_not_overwritten_by_the_run_s_choice():
    """A setup that gives each output sheet its own contract is answering a
    different question — "which contract governs Schedule A" — and one answer to
    "which contract is this file under" must not wipe out several of those.
    Both govern; each rule only fires on the sheets its own SQL names."""
    ids = dr._governing_ids({"Schedule A": 11, "Schedule B": 12},
                            eff_contract_id=20)
    assert ids == [11, 12, 20]


def test_the_governing_list_never_repeats_a_contract():
    """Rules are loaded with `contract_id = ANY(:ids)`. A repeat is harmless to
    SQL and not to the reader, who is shown this list."""
    assert dr._governing_ids({"Schedule A": 11}, eff_contract_id=11) == [11]


def test_a_setup_with_no_pins_is_governed_by_the_choice_alone():
    """The ordinary case, and the user's: one contract pinned by the setup, one
    chosen on the run, and only the chosen one's rules run."""
    assert dr._governing_ids({}, eff_contract_id=4019) == [4019]


def test_nothing_chosen_and_nothing_pinned_governs_nothing():
    """A programme with no contract at all still runs — the deterministic type
    checks need no contract. An empty list is a real answer, not a failure."""
    assert dr._governing_ids({}, eff_contract_id=None) == []


# ══════════════════════════════════════════════════════════════════════════
#  the check — a form field is a request, not a fact
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def world():
    """One programme, two brokers, and a contract apiece — plus one the carrier
    holds itself, which predates the broker level and governs the programme."""
    sfx = os.urandom(4).hex()
    with SessionLocal() as s:
        carrier = Tenant(tenant_name=f"runscope-{sfx}", legal_name="Runwright Re")
        s.add(carrier); s.commit()
        other = Tenant(tenant_name=f"runscope-x-{sfx}", legal_name="Elsewhere Re")
        s.add(other); s.commit()

        def party(name):
            p = Party(tenant_id=carrier.id, party_type="broker", legal_name=name,
                      reference=f"{name}-{sfx}", is_active=True)
            s.add(p); s.commit()
            return p

        ours, theirs = party("Ours"), party("Theirs")
        prog = Program(tenant_id=carrier.id, name=f"Runs {sfx}", is_app_managed=True)
        s.add(prog); s.commit()
        second = Program(tenant_id=carrier.id, name=f"Other {sfx}",
                         is_app_managed=True)
        s.add(second); s.commit()
        for b in (ours, theirs):
            s.add(ProgramBroker(tenant_id=carrier.id, program_id=prog.id,
                                broker_party_id=b.id, status="active"))
        s.commit()

        def contract(name, program_id, broker_party_id, tenant_id=None):
            c = Contract(tenant_id=tenant_id or carrier.id, name=name,
                         program_id=program_id, broker_party_id=broker_party_id,
                         is_app_managed=True)
            s.add(c); s.commit()
            return c

        return {
            "tid": carrier.id,
            "prog": prog.id,
            "ours": ours.id,
            "theirs": theirs.id,
            "mine": contract("Ours-v1", prog.id, ours.id).id,
            "not_mine": contract("Theirs-v1", prog.id, theirs.id).id,
            "carrier_held": contract("House terms", prog.id, None).id,
            "other_programme": contract("Elsewhere", second.id, ours.id).id,
        }


def _check(w, contract_id, broker=None):
    with SessionLocal() as s:
        return dr._assert_run_contract(s, w["tid"], w["prog"],
                                       broker if broker is not None else w["ours"],
                                       contract_id)


def test_this_broker_s_own_contract_is_accepted(world):
    assert _check(world, world["mine"]).id == world["mine"]


def test_a_carrier_held_contract_governs_whichever_broker_is_named(world):
    """It predates the broker level and still governs the programme. Refusing it
    would make an existing setup stop running."""
    assert _check(world, world["carrier_held"]).id == world["carrier_held"]
    assert _check(world, world["carrier_held"],
                  broker=world["theirs"]).id == world["carrier_held"]


def test_another_broker_s_contract_is_refused(world):
    """The failure this guard exists for: one broker's terms applied to another
    broker's bordereau, decided by nothing but a number in a form field."""
    with pytest.raises(HTTPException) as e:
        _check(world, world["not_mine"])
    assert e.value.status_code == 400
    assert "broker" in str(e.value.detail).lower()


def test_a_contract_from_another_programme_is_refused(world):
    with pytest.raises(HTTPException) as e:
        _check(world, world["other_programme"])
    assert e.value.status_code == 400
    assert "programme" in str(e.value.detail).lower()


def test_a_contract_that_does_not_exist_is_refused(world):
    with pytest.raises(HTTPException) as e:
        _check(world, 2_000_000_001)
    assert e.value.status_code == 400
