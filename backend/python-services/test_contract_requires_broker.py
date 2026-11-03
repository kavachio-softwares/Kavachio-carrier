"""A contract is always WITH one broker.

Uploading one in Bordereau Setup used to be allowed with the Broker left blank;
the contract then belonged to nobody, so every broker on the programme could see
it and be checked against it. The upload now refuses that before it reads
anything (carrier_scope.require_contract_broker, called first thing by
app_routes.program_contract_upload).

The route itself, the broker-on-this-programme checks that follow it, and the
typed-contract route are exercised on a copy of the database, not here: the app
loads its rule library from a database at import.

Run:  python -m pytest test_contract_requires_broker.py
"""
import os

# Never a real database: nothing here needs one, and an import must not find one.
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import HTTPException

import carrier_scope


def test_a_contract_with_no_broker_is_refused_in_plain_words():
    with pytest.raises(HTTPException) as e:
        carrier_scope.require_contract_broker(None)
    assert e.value.status_code == 400
    assert "broker" in e.value.detail.lower()
    assert "cannot be saved without one" in e.value.detail


def test_a_contract_that_names_a_broker_is_let_through():
    assert carrier_scope.require_contract_broker(12) is None
