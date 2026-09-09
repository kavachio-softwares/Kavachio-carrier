"""
test_contract_terms.py — the vocabulary a contract is written in.

Pure module tests: no database, no HTTP, no fixtures. Everything here is a
function of its arguments, which is the point — the terms a carrier types
become a clause and a check by arithmetic, not by interpretation, and that is
exactly the part worth pinning down.

Two things are being guarded:

  · A LIMIT IS THREE THINGS AT ONCE — a question on the form, a sentence in the
    contract, and a check on every row. Adding one to contract_types.py and
    forgetting either of the other two is the failure mode, and it is silent:
    the term is agreed, printed, signed, and then never checked.

  · A TERM IS TWO DATES. The duration offered by the form is a way of saying
    the second one and is never stored, so there is nothing here that could
    disagree with the dates later.
"""
from pathlib import Path

from dotenv import load_dotenv

# db.py builds its engine at import time and only main.py loads the .env, so the
# one test below that reaches the route module needs the environment first. The
# same two lines main.py runs — deliberately not `import main`, which stands a
# whole FastAPI app up to read one dictionary.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

import pytest

import contract_routes
import contract_rules as cr
import contract_types as ct
import contract_wording as cw
import esign_pdf

# A template mapped to the data model, as the bordereau setup hands it over.
TEMPLATE = [
    {"name": "Risk Country", "canonical_field": "risk_country", "sheet": "Risk"},
    {"name": "Risk Type", "canonical_field": "risk_type", "sheet": "Risk"},
    {"name": "Sum Insured", "canonical_field": "sum_insured", "sheet": "Risk"},
]


def _rules(**raw):
    limits = ct.clean_agreed_limits({k: {"value": v} for k, v in raw.items()})
    rules, unmapped = cr.map_limits_to_template(limits, TEMPLATE)
    return limits, {r["key"]: r for r in rules}, {u["key"]: u for u in unmapped}


# ── where business may NOT be written ───────────────────────────────────────

def test_an_exclusion_is_kept_with_teeth_by_default():
    """The opposite of the permitted list is not the same statement inverted:
    "we will not touch Crimea" is a refusal, not a preference."""
    limits, _, _ = _rules(excluded_territory="Crimea, Belarus")
    assert limits["excluded_territory"]["value"] == "Crimea, Belarus"
    assert limits["excluded_territory"]["severity"] == "critical"


def test_an_excluded_territory_becomes_a_clause_of_its_own():
    secs = cw.build_sections(values={}, limits=ct.clean_agreed_limits(
        {"excluded_territory": {"value": "Northern Ireland"}}))
    cover = next(s for s in secs if s["key"] == "cover")
    assert "{{excluded_territory}}" in cover["body"]
    # The VALUE is never baked into the sentence — that is what keeps the
    # wording and the check tied to one number.
    assert "Northern Ireland" not in cover["body"]


def test_a_contract_may_say_where_business_may_and_may_not_be_written():
    """"Anywhere in the EU except Malta" is one of each, and is the commonest
    shape of all — so neither sentence may displace the other."""
    secs = cw.build_sections(values={}, limits=ct.clean_agreed_limits({
        "territory": {"value": "the European Union"},
        "excluded_territory": {"value": "Malta"},
    }))
    body = next(s for s in secs if s["key"] == "cover")["body"]
    # The TOKENS, not the sentences around them. What matters is that both
    # terms reach the wording and in a readable order; how each clause is
    # phrased is prose, and pinning prose in a test means every improvement to
    # it arrives as a failure. See build_sections on how long a clause is.
    assert "{{territory}}" in body
    assert "{{excluded_territory}}" in body
    # Read in the order a person compares them: the permitted list, then the
    # carve-out from it.
    assert body.index("{{territory}}") < body.index("{{excluded_territory}}")


def test_an_excluded_territory_becomes_a_not_in_check():
    _, rules, unmapped = _rules(excluded_territory="Crimea, Belarus")
    assert not unmapped, unmapped
    r = rules["excluded_territory"]
    assert r["operator"] == "not_in"
    # Written as prose, so it is split the way prose separates things.
    assert r["operand"] == ["Crimea", "Belarus"]
    assert r["canonical_field"] == "risk_country"
    assert r["severity"] == "critical"
    assert "excluded by the contract" in r["error_message"]


def test_the_two_territory_limits_check_one_column_in_opposite_directions():
    _, rules, _ = _rules(territory="United Kingdom",
                         excluded_territory="Northern Ireland")
    assert rules["territory"]["column"] == rules["excluded_territory"]["column"]
    assert rules["territory"]["operator"] == "in"
    assert rules["excluded_territory"]["operator"] == "not_in"


def test_a_template_without_a_territory_column_says_so_rather_than_guessing():
    """Bound to the nearest-looking column it would fail rows for the wrong
    reason, which is worse than not checking because people act on it."""
    limits = ct.clean_agreed_limits({"excluded_territory": {"value": "Malta"}})
    rules, unmapped = cr.map_limits_to_template(
        limits, [{"name": "Premium", "canonical_field": "gross_written_premium"}])
    assert rules == []
    assert unmapped[0]["key"] == "excluded_territory"
    assert unmapped[0]["question"] == "Where business may not be written"


# ── the three lists that have to agree ──────────────────────────────────────

def test_every_checkable_limit_knows_a_column_and_an_operator():
    """THE test that matters most in this file. A limit with a `check` but no
    column silently produces no rule: agreed, printed, signed, never checked."""
    missing_column = sorted(k for k, s in ct.AGREED_LIMITS.items()
                            if s.get("check") and k not in cr.LIMIT_COLUMNS)
    missing_operator = sorted(k for k, s in ct.AGREED_LIMITS.items()
                              if s.get("check") and k not in cr.LIMIT_OPERATORS)
    assert missing_column == [], missing_column
    assert missing_operator == [], missing_operator


def test_nothing_is_mapped_that_is_not_a_limit():
    """The other direction: a column mapping left behind by a renamed limit
    maps nothing, and looks like coverage that is not there."""
    assert not set(cr.LIMIT_COLUMNS) - set(ct.AGREED_LIMITS)
    assert not set(cr.LIMIT_OPERATORS) - set(ct.AGREED_LIMITS)


def test_the_form_is_told_which_limits_to_offer_first():
    """The frontend held this list, so a limit added here appeared in the
    wording and in the checks but not on the screen somebody would type it on."""
    served = {l["name"]: l for l in ct.agreed_limits_spec()}
    assert set(ct.COMMON_LIMITS) <= set(ct.AGREED_LIMITS), "a term nobody can set"
    assert served["excluded_territory"]["common"] is True
    assert served["territory"]["common"] is True
    assert {k for k, v in served.items() if v["common"]} == set(ct.COMMON_LIMITS)
    # Every limit is still served — `common` decides what is shown first, not
    # what exists.
    assert set(served) == set(ct.AGREED_LIMITS)


def test_a_limit_that_cannot_be_checked_never_claims_to_stop_a_row():
    limits = ct.clean_agreed_limits({"tax_treatment": {
        "value": "inclusive of taxes", "severity": "critical"}})
    assert limits["tax_treatment"]["severity"] is None


# ── how long a term runs ────────────────────────────────────────────────────

def test_the_term_lengths_are_offered_from_one_month_to_five_years():
    durations = ct.term_spec()["durations"]
    months = [d["months"] for d in durations]
    assert months == sorted(months), "a picker in a jumbled order"
    assert len(set(months)) == len(months)
    assert months[0] == 1 and months[-1] == 60


def test_every_length_is_labelled_the_way_somebody_would_say_it():
    labels = {d["months"]: d["label"] for d in ct.term_spec()["durations"]}
    assert labels[1] == "1 month"
    assert labels[6] == "6 months"
    assert labels[12] == "1 year"
    assert labels[24] == "2 years"
    assert labels[60] == "5 years"


def test_a_term_counts_both_of_its_days():
    """The wording says so in as many words, so the form has to count the same
    way or a twelve-month contract prints one date and expires on another."""
    assert ct.term_spec()["inclusive"] is True
    secs = cw.build_sections(
        values={"inception_dt": "2027-01-01", "expiry_dt": "2027-12-31"},
        limits={})
    parties = next(s for s in secs if s["key"] == "parties")
    assert "both days inclusive" in parties["body"]


def test_a_duration_is_never_stored():
    """Two answers to one question is how they end up disagreeing. The dates
    are the fact; a duration is only a quicker way to type the second one."""
    assert not [k for k in ct.FIELDS if "duration" in k]
    assert not [k for k in ct.AGREED_LIMITS if "duration" in k]


def test_the_form_is_served_the_term_vocabulary():
    """The lengths and the convention reach the form the same way the fields
    do — served, so the two cannot drift."""
    payload = contract_routes.contract_types(None)
    assert payload["term"] == ct.term_spec()
    assert payload["term"]["durations"], "a picker with nothing in it"


def test_a_backwards_term_is_refused_wherever_it_is_typed():
    """Whatever the form works out, the server is the one that decides."""
    import pytest
    with pytest.raises(ct.ContractTypeError) as e:
        ct.validate("insurer_broker", {
            "name": "Schedule A", "counterparty_party_id": 1,
            "inception_dt": "2027-12-31", "expiry_dt": "2027-01-01",
            "class_of_business": "Property",
        })
    assert "expiry_dt" in e.value.errors
    assert "before inception" in e.value.errors["expiry_dt"]


# ── the words and the terms staying one contract ────────────────────────────

def _sec(body: str):
    return [{"key": "financial", "title": "Financial terms", "body": body}]


def test_a_typed_figure_is_tied_back_to_the_term_it_quotes():
    out, retied = cw.retie(_sec("Commission is payable at 11% of premium."),
                           {"commission_pct": "11%"})
    assert retied == ["commission_pct"]
    assert out[0]["body"] == "Commission is payable at {{commission_pct}} of premium."


def test_a_figure_inside_a_longer_one_is_left_alone():
    """"13%" must not be found in "113%", or a re-tie invents a term the
    sentence never quoted."""
    out, retied = cw.retie(_sec("A load of 113% applies, and 13% does not."),
                           {"commission_pct": "13%"})
    assert retied == ["commission_pct"]
    assert out[0]["body"] == "A load of 113% applies, and {{commission_pct}} does not."


def test_two_terms_agreed_at_the_same_figure_are_left_alone():
    """Which one the sentence meant is genuinely unknowable, and guessing would
    tie a commission clause to the brokerage rate."""
    out, retied = cw.retie(_sec("Commission is payable at 15%."),
                           {"commission_pct": "15%", "brokerage_pct": "15%"})
    assert retied == []
    assert "15%" in out[0]["body"] and "{{" not in out[0]["body"]


def test_a_clause_that_still_has_its_chip_is_untouched():
    body = "Commission is payable at {{commission_pct}}."
    out, retied = cw.retie(_sec(body), {"commission_pct": "11%"})
    assert retied == [] and out[0]["body"] == body


def test_a_one_character_value_is_never_tied():
    """Too short to be anything but a coincidence — a term worth 5 would claim
    every 5 in the document."""
    out, retied = cw.retie(_sec("Not more than 5 named insureds."),
                           {"policy_period_months": "5"})
    assert retied == [] and "5 named" in out[0]["body"]


def test_a_term_the_wording_does_not_quote_is_named():
    limits = ct.clean_agreed_limits({"commission_pct": {"value": 11},
                                     "currency": {"value": "USD"}})
    sections = _sec("Commission is payable at {{commission_pct}}.")
    assert cw.unquoted_terms(sections, limits) == ["currency"]


def test_the_drift_shows_up_where_the_other_second_looks_do():
    """It rides on derive_checks, so step 3, the preview and the record all get
    it without any of them knowing how it is worked out."""
    limits = ct.clean_agreed_limits({"commission_pct": {"value": 11}})
    _, warnings = cw.derive_checks({}, limits, _sec("Nothing about commission."))
    assert any("does not quote" in w["title"] for w in warnings), warnings
    assert any("11%" in w["detail"] for w in warnings), warnings
    # Not asked, not answered: the same call without the sections says nothing
    # about them, because a caller that has no wording is not one that lost it.
    _, quiet = cw.derive_checks({}, limits)
    assert not [w for w in quiet if "does not quote" in w["title"]]


# ── how a clause reads ──────────────────────────────────────────────────────

def _every_clause() -> list[str]:
    """Every sentence this builder can produce, rendered, for a contract that
    sets everything it is possible to set."""
    limits = {}
    for key, spec in ct.AGREED_LIMITS.items():
        kind = spec["kind"]
        value = {"percent": 12, "money": 100000, "int": 6}.get(
            kind, "the European Union")
        if spec.get("choices"):
            value = spec["choices"][0]
        limits[key] = {"value": value}
    limits = ct.clean_agreed_limits(limits)
    values = {"name": "Whole", "inception_dt": "2026-01-01",
              "expiry_dt": "2026-12-31", "class_of_business": "Property",
              "notice_period_days": 30}
    tokens = cw.token_values(values=values, limits=limits,
                             carrier_name="A Carrier",
                             counterparty_name="A Broker")
    out = []
    for section in cw.build_sections(values=values, limits=limits):
        for line in section["body"].split("\n"):
            # Drop the clause number the builder hangs in the margin.
            out.append(cw.render(line.split("  ", 1)[-1], tokens))
    return out


def test_no_clause_is_too_short_to_be_understood_on_its_own():
    """"It covers C." is a true sentence that answers nothing. A reader who has
    to hold the schedule beside the wording to work out what a clause means will
    not read the wording, so every clause states what it applies to and what
    follows from it."""
    short = [c for c in _every_clause() if len(c) < 55]
    assert not short, short


def test_and_none_of_them_runs_past_a_line():
    """The other failure. A clause nobody can read for being too long is not
    better than one nobody can use for being too short — past about a line on
    the page a clause stops being read, and anything needing more than that is
    a second clause."""
    long = [c for c in _every_clause() if len(c) > 190]
    assert not long, long


# ── what the form does NOT ask for ──────────────────────────────────────────

def test_a_market_reference_is_never_asked_for_when_raising_a_contract():
    """UMR, Lloyd's risk code and section number are issued elsewhere — by the
    market, not by the two parties filling this form in. Asking produced a blank
    on nearly every contract, and the few that were filled in held a
    placeholder, which is worse than a blank because it looks like an answer."""
    for key in ct.CONTRACT_TYPES:
        asked = set(ct.field_names(key))
        assert not asked & {"umr", "risk_code", "section_number"}, key
    assert not {"umr", "risk_code", "section_number"} & set(ct.FIELDS)


def test_the_endpoint_does_not_accept_one_either():
    """A field the form cannot ask for is a field the API should not take. It
    used to sit on the create body doing nothing — accepted, then dropped on the
    floor by create_contract — which reads to a caller like a value that was
    saved. `umr` is left on the body deliberately: the record still shows it,
    so a caller that has one has somewhere to put it."""
    taken = set(contract_routes.ContractIn.model_fields)
    assert not taken & {"risk_code", "section_number"}


def test_but_a_contract_that_has_one_still_prints_it():
    """Not asked for is not the same as not carried. An uploaded wording is
    read for these, a renewal inherits them from the contract it succeeds, and
    the schedule prints whichever are set."""
    schedule = cw.schedule_rows(
        values={"risk_code": "B4", "section_number": "2",
                "class_of_business": "Property"},
        limits={}, type_label=None, programme_name=None)
    printed = {label: value
               for _group, rows in schedule for label, value in rows}
    assert printed.get("Risk code") == "B4"
    assert printed.get("Section") == "2"
    assert printed.get("Class of business") == "Property"


# ── where the signature blocks go ───────────────────────────────────────────
#
# A contract can be signed in three arrangements and only the third can be got
# wrong: side by side and stacked are drawn by the document builder from nothing
# but a name, whereas a hand-placed block is a page number and a point somebody
# chose, and every one of those can be a page that no longer exists or a corner
# that hangs the block off the paper. What is guarded here is that a bad
# placement is refused while it is being made and survived when it is read back
# — a contract with nowhere to sign is the one failure worse than the wrong
# layout, and it is silent.

def _placed(**blocks):
    return {"arrangement": "placed", "blocks": blocks}


def test_a_block_can_be_placed_by_hand():
    lay = esign_pdf.normalise_signature_layout(
        _placed(carrier={"page": 2, "x": 0.1, "y": 0.55},
                counterparty={"page": 2, "x": 0.55, "y": 0.55}),
        strict=True)
    assert lay["arrangement"] == "placed"
    assert lay["blocks"]["carrier"] == {"page": 2, "x": 0.1, "y": 0.55}
    assert lay["blocks"]["counterparty"]["x"] == 0.55


def test_placing_one_block_and_not_the_other_is_refused_while_it_is_being_made():
    with pytest.raises(esign_pdf.SignatureLayoutError) as e:
        esign_pdf.normalise_signature_layout(
            _placed(carrier={"page": 1, "x": 0.1, "y": 0.5}), strict=True)
    assert "counterparty" in e.value.errors


def test_but_a_half_placed_layout_already_stored_still_gets_a_signature_page():
    """The read path never raises. A contract whose layout cannot be drawn as
    asked falls back to the arrangement every contract has always had, because
    the alternative is a document with nowhere to sign."""
    lay = esign_pdf.normalise_signature_layout(
        _placed(carrier={"page": 1, "x": 0.1, "y": 0.5}))
    assert lay["arrangement"] == esign_pdf.DEFAULT_ARRANGEMENT


def test_a_block_dragged_off_the_page_is_refused():
    with pytest.raises(esign_pdf.SignatureLayoutError):
        esign_pdf.normalise_signature_layout(
            _placed(carrier={"page": 1, "x": 1.4, "y": 0.5},
                    counterparty={"page": 1, "x": 0.5, "y": 0.5}),
            strict=True)


def test_a_placement_survives_a_look_at_the_other_arrangements():
    """Switching to side-by-side to see how it reads and back again must not
    throw away where the blocks were put — the two are different questions."""
    lay = esign_pdf.normalise_signature_layout(
        {"arrangement": "stacked",
         "blocks": {"carrier": {"page": 3, "x": 0.1, "y": 0.2},
                    "counterparty": {"page": 3, "x": 0.5, "y": 0.2}}},
        strict=True)
    assert lay["arrangement"] == "stacked"
    assert lay["blocks"]["carrier"]["page"] == 3


def test_the_form_is_told_how_big_a_placed_block_is():
    """The box dragged on the screen and the block drawn on the page are one
    number, served — two copies of it is how somebody places one thing and gets
    another."""
    spec = esign_pdf.signature_block_spec()
    assert "placed" in {a["key"] for a in spec["arrangements"]}
    assert 0 < spec["placed_block"]["width"] <= 1
    assert 0 < spec["placed_block"]["height"] <= 1


def _compose(layout, **kw):
    values = {"name": "Placed", "inception_dt": "2026-01-01",
              "expiry_dt": "2026-12-31", "class_of_business": "Property"}
    limits = ct.clean_agreed_limits({"commission_pct": {"value": 11},
                                     "currency": {"value": "USD"}})
    sections = cw.build_sections(values=values, limits=limits)
    return cw.compose_pdf(
        name="Placed", carrier_name="A Carrier", counterparty_name="A Broker",
        sections=sections,
        tokens=cw.token_values(values=values, limits=limits),
        schedule=cw.schedule_rows(values=values, limits=limits),
        signature_layout=esign_pdf.normalise_signature_layout(layout),
        **kw)


def test_a_placed_block_puts_its_signing_boxes_where_it_was_dropped():
    """The point of placing by hand: the box a signer is given is where the
    carrier left the block, not where a page layout decided to put it."""
    pdf = _compose(_placed(carrier={"page": 1, "x": 0.1, "y": 0.6},
                           counterparty={"page": 1, "x": 0.55, "y": 0.6}),
                   anchors={"carrier": "tenant:1", "counterparty": "broker:2"})
    sigs = {f.party_key: f for f in esign_pdf.discover_fields(pdf)
            if f.type == "signature"}
    assert set(sigs) == {"tenant:1", "broker:2"}
    for key, x in (("tenant:1", 0.1), ("broker:2", 0.55)):
        assert sigs[key].page == 1
        assert abs(sigs[key].x - x) < 0.02, sigs[key]
        # Above the ruled line, so a stamped signature sits ON it.
        assert 0.6 < sigs[key].y < 0.66, sigs[key]


def test_the_pages_do_not_change_when_the_arrangement_does():
    """A position is a page number, so an arrangement that shortened the
    document would silently point every placement one page too far."""
    auto = _compose({"arrangement": "side_by_side"})
    placed = _compose(_placed(carrier={"page": 1, "x": 0.1, "y": 0.6},
                              counterparty={"page": 1, "x": 0.55, "y": 0.6}))
    assert esign_pdf.page_count(auto) == esign_pdf.page_count(placed)


def test_a_block_placed_past_the_end_is_drawn_on_the_last_page():
    """The wording shrank after the block was placed. Drawing it on the last
    page is wrong in a way somebody can see and fix; dropping it leaves a
    contract that cannot be signed and says nothing about why."""
    pdf = _compose(_placed(carrier={"page": 99, "x": 0.1, "y": 0.5},
                           counterparty={"page": 99, "x": 0.55, "y": 0.5}),
                   anchors={"carrier": "tenant:1", "counterparty": "broker:2"})
    last = esign_pdf.page_count(pdf)
    assert {f.page for f in esign_pdf.discover_fields(pdf)} == {last}


def test_a_draft_download_of_a_placed_contract_carries_no_boxes():
    """Only the copy going out for signature is tagged. The draft anybody can
    download is the same document with nothing to click on."""
    pdf = _compose(_placed(carrier={"page": 1, "x": 0.1, "y": 0.6},
                           counterparty={"page": 1, "x": 0.55, "y": 0.6}))
    assert esign_pdf.discover_fields(pdf) == []
