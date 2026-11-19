"""The heuristic label rules (exporter._HEURISTIC_RULES) for transaction dates.

Every Lloyd's v5.2 output template used to label "Effective Date of
Transaction" as the POLICY effective date: no rule named the transaction's
own dates, so the heading fell through to the generic "effective date" rule.
The heuristic pins with confidence 1.0, so it overrode the AI, which had the
right answer second. Previous Submission Reconciliation (task 23) then found
no transaction effective date and compared nothing. Pure: no database."""
from __future__ import annotations

import pytest

import exporter
import mapper

TXN_EFF, TXN_EXP = "premium_transaction_effective_date", "premium_transaction_expiry_date"


@pytest.mark.parametrize("heading", [
    "Effective Date of Transaction", "Effective Date of Transaction US",
    "Transaction Effective Date", "TransactionEffectiveDate", "Trans Eff Date",
    "Txn Effective Date",
])
def test_a_transactions_effective_date_is_the_transactions(heading):
    assert exporter._heuristic_pin(heading, []) == TXN_EFF
    assert mapper._heuristic_pin(heading, []) == TXN_EFF      # input side agrees


@pytest.mark.parametrize("heading", [
    "Expiry Date of Transaction", "Transaction Expiry Date", "TransactionExpirationDate",
])
def test_a_transactions_expiry_date_is_the_transactions(heading):
    assert exporter._heuristic_pin(heading, []) == TXN_EXP
    assert mapper._heuristic_pin(heading, []) == TXN_EXP


@pytest.mark.parametrize("heading, field", [
    ("Policy Effective Date", "policy_effective_date"),
    ("Risk Inception Date", "policy_effective_date"),
    ("Effective Date", "policy_effective_date"),
    ("Risk Expiry Date", "policy_expiration_date"),
    ("Policy Expiration", "policy_expiration_date"),
    ("Transaction Date", TXN_EFF),
    ("Transaction Type", "premium_transaction_type"),
    ("Transit Effective Date", "policy_effective_date"),     # 'transit' is not a transaction
])
def test_other_dates_keep_their_labels(heading, field):
    assert exporter._heuristic_pin(heading, []) == field


def test_the_new_targets_are_fields_a_template_can_carry():
    assert {TXN_EFF, TXN_EXP} <= exporter._EXPORTABLE_FIELDS
