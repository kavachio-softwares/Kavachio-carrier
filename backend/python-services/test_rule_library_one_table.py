"""The Rule Library screen and Bordereau Setup read ONE table.

They used not to. The screen wrote through GenericRuleSpecification, which the
data-model migration moved to `generic_rule_spec`; the loader Bordereau Setup
calls (load_generic_rules) kept reading `generic_rule_specification` in raw
SQL. A rule a carrier admin added, edited or switched off therefore never
reached a setup, and Kavachio's screen listed none of the rules that did run.

These tests write rules the way the screen does — through the model — and
assert the loader hands exactly the in-scope ones to the pipeline.

Self-contained: an in-memory SQLite database, no shared database touched.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db
from db import GenericRuleSpecification as G
from contract_upload_services import generic_rule_library as grl

CARRIER_A, CARRIER_B = 7, 8


@pytest.fixture
def library(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    G.__table__.create(engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(db, "SessionLocal", Session)

    def add(name, tenant_id=None, is_active=True, is_generic=True):
        with Session() as s:
            r = G(rule_name=name, class_name="NotNull", severity="Major",
                  validation_logic=f"{name} must be present.",
                  tenant_id=tenant_id, is_active=is_active, is_generic=is_generic)
            s.add(r); s.commit()
            return r.id
    return add


def _names(rules):
    return [r["rule_name"] for r in rules]


def test_a_rule_saved_by_the_screen_reaches_the_setup(library):
    rid = library("Policy Number Must Be Present", tenant_id=CARRIER_A)
    rules = grl.load_generic_rules(CARRIER_A)
    assert [r["id"] for r in rules] == [rid]
    assert set(rules[0]) == {"id", "rule_name", "severity", "class_name",
                             "validation_logic", "tenant_id"}


def test_a_carrier_gets_the_platform_rules_plus_its_own_only(library):
    library("Global one")
    library("Carrier A rule", tenant_id=CARRIER_A)
    library("Carrier B rule", tenant_id=CARRIER_B)
    assert _names(grl.load_generic_rules(CARRIER_A)) == ["Global one", "Carrier A rule"]
    assert _names(grl.load_generic_rules(CARRIER_B)) == ["Global one", "Carrier B rule"]
    # No carrier (a Kavachio-side run): platform rules alone.
    assert _names(grl.load_generic_rules(None)) == ["Global one"]


def test_switched_off_rules_do_not_run(library):
    library("On")
    library("Off", is_active=False)
    library("Not generic", is_generic=False)
    assert _names(grl.load_generic_rules(CARRIER_A)) == ["On"]


def test_the_rule_reaches_the_mapper_as_an_intent(library):
    rid = library("Insured Name Must Be Present", tenant_id=CARRIER_A)
    clauses, intents, rules = grl.build_generic_intents(
        CARRIER_A, [{"field": "Insured Name"}])
    assert [c["clause_id"] for c in clauses] == [-rid]
    assert intents[0]["intents"][0]["operator"] == "required"


def test_an_empty_library_says_so(library, capsys):
    assert grl.load_generic_rules(CARRIER_A) == []
    assert "migrations/30_generic_rule_library_one_table.sql" in capsys.readouterr().out
