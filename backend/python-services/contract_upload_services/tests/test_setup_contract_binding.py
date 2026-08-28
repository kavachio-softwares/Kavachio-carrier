"""
test_setup_contract_binding.py
──────────────────────────────
A bordereau setup shows a contract's rules by looking up the contract BOUND to
it (`direct_format.contract_id` / `.sheet_contracts`). That binding is written
from a single value the browser reads out of the contract-upload response.

The failure this pins: extraction can outlive an ingress request cap, so the
browser is left with a 200 whose body is only heartbeat whitespace. The server
task is NOT cancelled — it finishes and persists the contract and its rules
minutes later — but the browser has no id, so the setup is saved with NO
binding. Every generated rule then becomes invisible, behind a green
"Mapping Generated" screen reading "0 rules across 0 of N fields".

Observed on qa: contract 119 held 16 active rules while its setup (direct_format
120) had `contract_id = NULL`, and the screen showed zero.

Two halves are tested:
  * `_attach_clauses` — what each binding state renders (including the two
    states that render zero, which is what made the loss invisible); and
  * `_unbound_setup_contract_id` — the fallback that resolves the contract for a
    setup carrying NO binding at all, so a lost response cannot hide the rules.

Pure: `_attach_clauses` is exercised against a stubbed rule store, and the
fallback against a stubbed session. No DB, no network.

Run standalone:  python contract_upload_services/tests/test_setup_contract_binding.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

# Importing the route module builds the service's DB engine and Gemini client,
# so the usual service environment has to be present. Nothing here reads or
# writes application data — every DB touch below is stubbed.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:
    pass
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@127.0.0.1:5432/none")

import direct_routes as dr  # noqa: E402

FIELDS = ["Policy Effective Date", "Insured Name", "Gross Written Premium"]


def _fields(*sheets):
    return [{"sheet": sh, "field": f} for sh in sheets for f in FIELDS]


def _stub_clause_store(by_contract):
    """Stand in for the DB read: {contract_id: {field: [clause, ...]}}."""
    def _lookup(contract_id, field_names):
        return by_contract.get(contract_id, {})
    return _lookup


def _rules(contract_id, *fields):
    return {f: [{"rule_id": contract_id * 100 + i, "rule_name": f"r{i}",
                 "scoped": False, "sql_sheets": []}]
            for i, f in enumerate(fields)}


def _attach(fields, contract_id, sheet_contracts, store):
    original = dr._contract_clauses_by_field
    dr._contract_clauses_by_field = _stub_clause_store(store)
    try:
        dr._attach_clauses(fields, contract_id, sheet_contracts)
    finally:
        dr._contract_clauses_by_field = original
    ids = {c["rule_id"] for f in fields for c in f["clauses"]}
    with_rules = sum(1 for f in fields if f["clauses"])
    return len(ids), with_rules


def test_bound_setup_shows_its_rules():
    """The healthy state: a fallback contract, no per-sheet pins."""
    store = {119: _rules(119, *FIELDS)}
    n, with_rules = _attach(_fields("Sheet1"), 119, {}, store)
    assert (n, with_rules) == (3, 3), (n, with_rules)


def test_unbound_setup_shows_nothing():
    """The observed failure: the rules exist, the binding does not."""
    store = {119: _rules(119, *FIELDS)}
    n, with_rules = _attach(_fields("Sheet1"), None, {}, store)
    assert (n, with_rules) == (0, 0), (n, with_rules)
    n, with_rules = _attach(_fields("Sheet1"), None, None, store)
    assert (n, with_rules) == (0, 0), (n, with_rules)


def test_pin_to_absent_sheet_also_shows_nothing():
    """The other zero-rendering state: the contract is pinned to a sheet the
    template does not have, and a pinned contract is deliberately not also the
    fallback — so no sheet claims it. Pinned semantics are intentional (a
    schedule contract must not leak onto other schedules); this test exists so
    the zero is a KNOWN outcome rather than a surprise."""
    store = {119: _rules(119, *FIELDS)}
    n, with_rules = _attach(_fields("Sheet1"), 119, {"Schedule H": 119}, store)
    assert (n, with_rules) == (0, 0), (n, with_rules)


def test_pinned_contract_stays_on_its_own_sheet():
    """Two contracts, one pinned per sheet — neither leaks onto the other."""
    store = {119: _rules(119, *FIELDS), 120: _rules(120, *FIELDS)}
    fields = _fields("Sheet1", "Schedule H")
    _attach(fields, None, {"Sheet1": 119, "Schedule H": 120}, store)
    s1 = {c["rule_id"] // 100 for f in fields if f["sheet"] == "Sheet1"
          for c in f["clauses"]}
    sh = {c["rule_id"] // 100 for f in fields if f["sheet"] == "Schedule H"
          for c in f["clauses"]}
    assert s1 == {119}, s1
    assert sh == {120}, sh


# ── the fallback ────────────────────────────────────────────────────────────

class _StubQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _StubSession:
    def __init__(self, rows):
        self._rows = rows
        self.queried = False

    def query(self, *a, **k):
        self.queried = True
        return _StubQuery(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _with_session(rows):
    sess = _StubSession(rows)
    original = dr.SessionLocal
    dr.SessionLocal = lambda: sess
    try:
        return dr._unbound_setup_contract_id(696, 752), sess
    finally:
        dr.SessionLocal = original


def test_fallback_resolves_the_programs_active_contract():
    got, _ = _with_session([(119,)])
    assert got == 119, got


def test_fallback_is_none_when_the_program_has_no_active_contract():
    got, _ = _with_session([])
    assert got is None, got


def test_fallback_needs_both_program_and_template():
    """Never guess from half a scope — an unscoped lookup could return another
    program's contract."""
    original = dr.SessionLocal
    dr.SessionLocal = lambda: _StubSession([(119,)])
    try:
        assert dr._unbound_setup_contract_id(None, 752) is None
        assert dr._unbound_setup_contract_id(696, None) is None
    finally:
        dr.SessionLocal = original


def test_fallback_survives_a_failed_lookup():
    """A page must still render if the lookup raises."""
    class _Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *a):
            return False

    original = dr.SessionLocal
    dr.SessionLocal = lambda: _Boom()
    try:
        assert dr._unbound_setup_contract_id(696, 752) is None
    finally:
        dr.SessionLocal = original


def test_fallback_recovers_the_qa_case_end_to_end():
    """direct_format 120: contract_id NULL, no pins, program 111 holding active
    contract 119 with rules → the setup shows 119's rules instead of zero."""
    store = {119: _rules(119, *FIELDS)}
    contract_id, sheet_contracts = None, None
    if not contract_id and not (sheet_contracts or {}):
        contract_id, _ = _with_session([(119,)])
    n, with_rules = _attach(_fields("Sheet1"), contract_id, sheet_contracts, store)
    assert (n, with_rules) == (3, 3), (n, with_rules)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print(f"\n{'FAILED' if fails else 'all passed'} ({fails} failure(s))")
    sys.exit(1 if fails else 0)
