"""
test_postal_country_scope.py
────────────────────────────
Tests for the two halves of the postal data-quality derivation:

  * the shared column-role tests (`uszips_reference.is_state_column` /
    `is_postal_column`), which decide whether a STATE / ZIP rule exists at all
    and whether the reference table is loaded for it; and
  * `validation_rule_generator.contract_postal_countries` + the country ladder
    inside `derive_formula_entries`, which decide WHICH country's regions and
    postal codes a row is judged against.

The case that motivated them: a Lloyd's-style BDX whose headers carry no
separators ("InsuredState", "RiskState", "RiskCountry"). The token test split
only on non-alphanumerics, so "InsuredState" was the single opaque token
"insuredstate" — no state rule was emitted for ANY column of that bordereau, and
the contract's own "COUNTRY OF ORIGIN: United Kingdom" had nowhere to land.

Both pieces are pure — no DB and no LLM.

Run standalone:  python contract_upload_services/tests/test_postal_country_scope.py
(or under pytest — each case asserts independently).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

# The derivers are pure, but importing their module pulls in the service's
# DB-backed constants and the Gemini client, so the usual service environment has
# to be present. Nothing here reads or writes application data.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:
    pass
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")

from contract_upload_services.uszips_reference import (        # noqa: E402
    column_tokens, is_state_column, is_postal_column,
)
from contract_upload_services.intl_postal_reference import (   # noqa: E402
    resolve_country_code, postal_country_code,
)
from contract_upload_services.validation_rule_generator import (  # noqa: E402
    contract_postal_countries, derive_formula_entries,
)
from contract_upload_services.generic_rule_library import (     # noqa: E402
    drop_derived_duplicates, drop_uncountried_reference_rules,
)


def _fields(*names):
    return [{"name": n, "sheet": "Sheet1", "samples": [], "samples_all": []}
            for n in names]


def _country_rule(field, allowed, **extra):
    """The mapped shape a "COUNTRY OF ORIGIN: X" clause becomes in Call 3."""
    ir = {"template": "value_in_set", "params": {"field": field, "allowed": list(allowed)},
          "rule_name": f"{field} must be one of {allowed}"}
    ir["params"].update(extra.pop("params", {}))
    ir.update(extra)
    return {"clause": {"clause_id": 1, "text": f"COUNTRY OF ORIGIN: {allowed}"},
            "engine": "ir", "candidates": [ir]}


def _postal_rules(synth, *names):
    """{(template, state_field, zip_field): params} for the postal rules derived."""
    out = {}
    for e in derive_formula_entries(synth, _fields(*names)):
        ir = e["candidates"][0]
        if ir["template"] in ("state_validity", "zip_state_consistency"):
            p = ir["params"]
            out[(ir["template"], p.get("state_field"), p.get("zip_field"))] = p
    return out


# ── column roles: the separator-less headers that started this ───────────────
def test_camelcase_headers_are_state_columns():
    for name in ("InsuredState", "RiskState", "BrokerState", "USState",
                 "Insured State", "state_code", "Risk-State", "STATE"):
        assert is_state_column(name), name


def test_words_that_merely_contain_state_are_not_state_columns():
    for name in ("Real Estate", "Statement Date", "Interstate", "Restatement",
                 "RealEstate", "StatementDate", "RiskCounty"):
        assert not is_state_column(name), name


def test_every_spelling_of_a_postal_column_is_recognised():
    for name in ("InsuredZipCode", "Insured Zip Code", "Broker Zip", "ZIP",
                 "Postal Code", "PostalCode", "PostCode", "Postcode", "Post Code"):
        assert is_postal_column(name), name


def test_columns_that_are_not_postal_columns():
    for name in ("Posting Date", "Deposit Code", "Policy Number", "InsuredState"):
        assert not is_postal_column(name), name


def test_column_tokens_splits_camel_humps_and_separators():
    assert column_tokens("InsuredZipCode") == ["insured", "zip", "code"]
    assert column_tokens("state_code") == ["state", "code"]
    assert column_tokens("UMRState") == ["umr", "state"]


# ── country resolution comes from reference data, not a hardcoded list ───────
def test_country_spellings_resolve_through_the_reference():
    for spelling in ("United Kingdom", "uk", "U.K.", "GB", "GBR",
                     "Great Britain", "England", "Scotland"):
        assert postal_country_code(spelling) == "GB", spelling
    for spelling in ("USA", "United States", "US", "U.S.A."):
        assert postal_country_code(spelling) == "US", spelling


def test_a_country_without_postal_data_resolves_but_is_not_postal_checkable():
    assert resolve_country_code("France") == "FR"
    assert postal_country_code("France") is None


def test_a_value_that_names_no_country_resolves_to_nothing():
    for value in ("Worldwide", "Various", "N/A", ""):
        assert resolve_country_code(value) is None, value


# ── what the CONTRACT pins ──────────────────────────────────────────────────
def test_contract_country_is_read_off_the_mapped_country_rule():
    synth = [_country_rule("RiskCountry", ["United Kingdom"])]
    assert contract_postal_countries(synth, _fields("RiskCountry")) == ["GB"]


def test_no_country_rule_pins_nothing():
    assert contract_postal_countries([], _fields("RiskCountry")) is None


def test_a_non_country_allow_list_pins_nothing():
    synth = [_country_rule("RiskCountry", ["Worldwide"])]
    assert contract_postal_countries(synth, _fields("RiskCountry")) is None


def test_a_country_we_hold_no_postal_data_for_pins_an_empty_set():
    synth = [_country_rule("RiskCountry", ["France"])]
    assert contract_postal_countries(synth, _fields("RiskCountry")) == []


def test_a_mix_of_checkable_and_uncheckable_countries_falls_back():
    synth = [_country_rule("RiskCountry", ["United Kingdom", "France"])]
    assert contract_postal_countries(synth, _fields("RiskCountry")) is None


def test_a_scoped_or_referral_country_rule_pins_nothing():
    fields = _fields("RiskCountry")
    scoped = _country_rule("RiskCountry", ["United Kingdom"],
                           params={"scope": {"ClassofBusiness": ["Marine"]}})
    assert contract_postal_countries([scoped], fields) is None
    referral = _country_rule("RiskCountry", ["United Kingdom"], is_referral=True)
    assert contract_postal_countries([referral], fields) is None


# ── the ladder: contract country → BDX country column → no state rule ───────
_COLS = ("InsuredState", "RiskState", "BrokerState", "InsuredCountry",
         "RiskCountry", "BrokerCountry", "InsuredZipCode")


def test_separatorless_state_columns_now_get_a_rule_at_all():
    rules = _postal_rules([], *_COLS)
    for state in ("InsuredState", "RiskState", "BrokerState"):
        assert ("state_validity", state, None) in rules, state


def test_contract_country_wins_over_the_bdx_country_column():
    rules = _postal_rules([_country_rule("RiskCountry", ["United Kingdom"])], *_COLS)
    risk = rules[("state_validity", "RiskState", None)]
    assert risk["countries"] == ["GB"]
    # the contract, not the row, decides — so no per-row country column is bound
    assert "country_field" not in risk


def test_without_a_contract_country_the_bdx_column_dispatches_per_row():
    rules = _postal_rules([], *_COLS)
    assert rules[("state_validity", "RiskState", None)]["country_field"] == "RiskCountry"
    assert rules[("state_validity", "InsuredState", None)]["country_field"] == "InsuredCountry"


def test_a_transaction_party_state_is_not_pinned_to_the_risks_country():
    rules = _postal_rules([_country_rule("RiskCountry", ["United Kingdom"])], *_COLS)
    broker = rules[("state_validity", "BrokerState", None)]
    assert "countries" not in broker
    # it follows its OWN country column instead
    assert broker["country_field"] == "BrokerCountry"


def test_a_contract_country_with_no_postal_data_emits_no_risk_rule():
    rules = _postal_rules([_country_rule("RiskCountry", ["France"])], *_COLS)
    assert ("state_validity", "RiskState", None) not in rules
    assert ("state_validity", "InsuredState", None) not in rules
    # the broker's own state is unaffected — it was never judged against France
    assert ("state_validity", "BrokerState", None) in rules


# ── no country anywhere: no state rule, but keep the ZIP rule ───────────────
def test_no_country_anywhere_means_no_state_rule():
    # A state rule with no country resolves against the union of US/CA/GB, so a
    # region of any other country reads as invalid. Better no rule than that one.
    rules = _postal_rules([], "InsuredState", "RiskState", "BrokerState")
    assert not [k for k in rules if k[0] == "state_validity"]


def test_no_country_anywhere_still_gets_the_zip_rule():
    # The ZIP check stays: its per-country shape guard skips a code belonging to
    # none of them, and it validates the state column as half of the pair.
    rules = _postal_rules([], "InsuredZipCode", "InsuredState")
    params = rules[("zip_state_consistency", "InsuredState", "InsuredZipCode")]
    assert "countries" not in params and "country_field" not in params


def test_a_single_unqualified_country_column_answers_for_the_state():
    # The commonest shape: "State" + "Country", neither naming a party.
    rules = _postal_rules([], "State", "Country")
    assert rules[("state_validity", "State", None)]["country_field"] == "Country"


def test_a_lone_broker_country_is_not_read_as_the_risks_country():
    rules = _postal_rules([], "InsuredState", "Broker Country")
    assert ("state_validity", "InsuredState", None) not in rules


def test_two_unqualified_country_columns_stay_ambiguous():
    rules = _postal_rules([], "State", "Country", "Country of Domicile")
    assert ("state_validity", "State", None) not in rules


def test_one_country_column_covers_only_the_columns_it_belongs_to():
    # An insured country column answers for the insured's state, not the broker's.
    rules = _postal_rules([], "InsuredState", "BrokerState", "InsuredCountry")
    assert rules[("state_validity", "InsuredState", None)]["country_field"] \
        == "InsuredCountry"
    assert ("state_validity", "BrokerState", None) not in rules


def test_the_zip_rule_follows_the_same_ladder():
    pinned = _postal_rules([_country_rule("RiskCountry", ["United Kingdom"])], *_COLS)
    assert pinned[("zip_state_consistency", "InsuredState",
                   "InsuredZipCode")]["countries"] == ["GB"]
    free = _postal_rules([], *_COLS)
    assert free[("zip_state_consistency", "InsuredState",
                 "InsuredZipCode")]["country_field"] == "InsuredCountry"


def test_a_postcode_spelling_pairs_with_its_state_column():
    rules = _postal_rules([], "InsuredPostcode", "InsuredState")
    assert ("zip_state_consistency", "InsuredState", "InsuredPostcode") in rules


# ── the library rule wins the column, but not the country dispatch ──────────
def _derived_and_library(contract_synth, library_ir):
    """Run the real hand-off: derive the postal rules, then let a generic library
    rule claim the same column. Returns the library rule's params."""
    synth = list(contract_synth)
    synth.extend(derive_formula_entries(contract_synth, _fields(*_COLS)))
    generic = [{"clause": {"clause_id": -34, "text": "[Generic library] state check"},
                "engine": "ir", "candidates": [library_ir]}]
    drop_derived_duplicates(synth, generic)
    return library_ir["params"]


def test_a_library_rule_that_replaces_a_derived_one_inherits_its_country():
    params = _derived_and_library(
        [_country_rule("RiskCountry", ["United Kingdom"])],
        {"template": "state_validity", "params": {"state_field": "InsuredState"},
         "rule_name": "Insured State Code Must Be Valid"})
    assert params["countries"] == ["GB"]


def test_a_library_rule_inherits_the_bdx_country_column_too():
    params = _derived_and_library(
        [], {"template": "state_validity", "params": {"state_field": "InsuredState"},
             "rule_name": "Insured State Code Must Be Valid"})
    assert params["country_field"] == "InsuredCountry"


def test_a_library_rule_that_already_names_a_country_is_left_alone():
    params = _derived_and_library(
        [_country_rule("RiskCountry", ["United Kingdom"])],
        {"template": "state_validity",
         "params": {"state_field": "InsuredState", "countries": ["US"]},
         "rule_name": "Insured State Code Must Be Valid"})
    assert params["countries"] == ["US"]


def test_a_library_state_rule_with_no_country_to_use_is_dropped():
    # The deriver refuses to emit one; the library must not sneak it back in.
    generic = [{"clause": {"clause_id": -34, "text": "[Generic rule] state check"},
                "engine": "ir",
                "candidates": [{"template": "state_validity",
                                "params": {"state_field": "InsuredState"},
                                "rule_name": "Insured State Code Must Be Valid"}]}]
    assert drop_uncountried_reference_rules(generic) == 1
    assert generic == []


def test_a_library_zip_rule_with_no_country_is_kept():
    generic = [{"clause": {"clause_id": -35, "text": "[Generic rule] zip check"},
                "engine": "ir",
                "candidates": [{"template": "zip_state_consistency",
                                "params": {"zip_field": "InsuredZipCode",
                                           "state_field": "InsuredState"},
                                "rule_name": "Insured ZIP Code Must Be Valid"}]}]
    assert drop_uncountried_reference_rules(generic) == 0
    assert len(generic) == 1


def test_a_library_state_rule_that_inherited_a_country_survives():
    generic = [{"clause": {"clause_id": -34, "text": "[Generic rule] state check"},
                "engine": "ir",
                "candidates": [{"template": "state_validity",
                                "params": {"state_field": "InsuredState",
                                           "countries": ["GB"]},
                                "rule_name": "Insured State Code Must Be Valid"}]}]
    assert drop_uncountried_reference_rules(generic) == 0
    assert len(generic) == 1


def test_a_different_check_on_the_same_column_takes_no_country_dispatch():
    # A shape check ("2 letters") looks nothing up in a country's reference, so
    # handing it a country would be meaningless.
    params = _derived_and_library(
        [_country_rule("RiskCountry", ["United Kingdom"])],
        {"template": "pattern_check",
         "params": {"field": "InsuredState", "pattern": "^[A-Z]{2}$"},
         "rule_name": "Insured State Code Must Be Two Letters"})
    assert "countries" not in params and "country_field" not in params


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
    print("all passed" if not failures else f"{failures} failure(s)")
    sys.exit(1 if failures else 0)
