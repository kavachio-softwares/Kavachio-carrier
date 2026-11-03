"""
test_contract_margin_policy_totals.py
─────────────────────────────────────
19.3 "Margin per contract" and 19.2 "Policy totals", run end to end in an
in-memory DuckDB through the real compiler and the real `run_validation`:

  * the number-limit builders compile byte-identically with no margin / 0, and
    widen their bound by |bound| x pct when one is set;
  * a rule's effective tolerance is its own value, else the contract margin,
    with the pre-margin audit event counted as "own";
  * a formula rule without its own value now follows the contract margin (0 →
    exact to the cent) instead of the server 1% — only when the run passes
    rule_settings; without them nothing changes;
  * money limits on a summing amount are measured on each multi-row policy's
    total (min / max / range), single-row and unnumbered policies keep today's
    per-row check, scope is applied before grouping;
  * the in-memory recompile keeps the stored query whenever it is not safe.

Pure — see _offline.py (no DB) — and no model calls.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _offline                                                  # noqa: E402,F401

os.environ["KAVACHIO_VARIATION_RECONCILE"] = "0"

import duckdb                                                    # noqa: E402
import pytest                                                    # noqa: E402

from contract_upload_services import rule_compiler as rc         # noqa: E402
import contract_margin as cm                                     # noqa: E402
import duckdb_validation as dv                                   # noqa: E402

SHEET = "Policies"
COLS = ["Policy Number", "Transaction Type", "Coverage", "Gross Premium",
        "Sum Insured", "Net Premium", "Commission"]


class _NoSession:
    """run_validation records an audit row per rule; there is no database here
    and it swallows the failure, exactly as it does for a broken session."""


def _row(pol, ttype, prem, cov="CGL", si="1000000", net="", comm=""):
    return {"Policy Number": pol, "Transaction Type": ttype, "Coverage": cov,
            "Gross Premium": prem, "Sum Insured": si, "Net Premium": net,
            "Commission": comm}


def _blocks(rows):
    return [{"sheet": SHEET, "records": rows}]


def _rule(rule_id, template, params, *, contract_id=7, terms_key=None, **extra):
    ir = {"template": template, "params": params, "rule_name": f"r{rule_id}"}
    fields = {c: [SHEET] for c in COLS}
    spec = {"ir": ir, "compiled_sql": rc.compile_ir(ir, fields, SHEET), **extra}
    if terms_key:
        spec.update({"source": "contract_terms", "limit": terms_key,
                     "template": template, "params": params})
    else:
        spec["kind"] = "ir_v1"
    return {"rule_id": rule_id, "contract_id": contract_id,
            "rule_name": f"r{rule_id}", "severity": "warning",
            "rule_spec": spec, "error_message": None}


def _run(rows, rules, settings=None):
    out = dv.run_validation(_blocks(rows), rules, session=_NoSession(),
                            rule_settings=settings)
    assert not out["unprocessable"], out["unprocessable"]
    return out["exceptions"]


def _settings(margins=None, own=(), policy=True):
    return {"margins": margins if margins is not None else {7: 0.0},
            "own_tolerance_rule_ids": list(own),
            "policy_columns": {SHEET: "Policy Number"} if policy else {}}


# ── A.2: limit builders ─────────────────────────────────────────────────────

def _old_shape(template, p):
    """The pre-19.3 builder bodies, written out from the unchanged helpers."""
    f = p["field"]
    nn = rc._not_numeric_select(SHEET, f, p.get("scope"))
    sc = rc._scope_clause(p.get("scope"))
    if template == "max_limit":
        mx = rc._num_lit(p["max"])
        w = rc._and(f"{rc._num(f)} IS NOT NULL AND {rc._num(f)} > {mx}", sc)
        return rc._select(SHEET, f, f"{f} exceeds maximum {mx}", w, f) + "\nUNION ALL\n" + nn
    if template == "min_limit":
        mn = rc._num_lit(p["min"])
        w = rc._and(f"{rc._num(f)} IS NOT NULL AND {rc._num(f)} < {mn}", sc)
        return rc._select(SHEET, f, f"{f} below minimum {mn}", w, f) + "\nUNION ALL\n" + nn
    mn, mx = rc._num_lit(p["min"]), rc._num_lit(p["max"])
    if mn == mx:
        w = rc._and(f"{rc._num(f)} IS NOT NULL AND {rc._num(f)} <> {mn}", sc)
        return rc._select(SHEET, f, f"{f} must equal {mn}", w, f) + "\nUNION ALL\n" + nn
    w = rc._and(f"{rc._num(f)} IS NOT NULL AND ({rc._num(f)} < {mn} OR {rc._num(f)} > {mx})", sc)
    return rc._select(SHEET, f, f"{f} outside [{mn}, {mx}]", w, f) + "\nUNION ALL\n" + nn


@pytest.mark.parametrize("template,params", [
    ("max_limit", {"field": "Gross Premium", "max": 1000}),
    ("min_limit", {"field": "Gross Premium", "min": 500.5}),
    ("range_check", {"field": "Gross Premium", "min": 100, "max": 200}),
    ("range_check", {"field": "Commission", "min": 0.235, "max": 0.235}),
    ("max_limit", {"field": "Gross Premium", "max": 1000, "scope": {"Coverage": "CGL"}}),
])
@pytest.mark.parametrize("tol", ["absent", None, "", 0, 0.0])
def test_limit_builders_byte_identical_without_a_margin(template, params, tol):
    p = dict(params)
    if tol != "absent":
        p["tolerance_pct"] = tol
    assert rc._BUILDERS[template](SHEET, p) == _old_shape(template, params)


def _flags(template, params, values):
    con = duckdb.connect(":memory:")
    con.execute(f'CREATE TABLE "{SHEET}" (__rowid INTEGER, "{params["field"]}" VARCHAR)')
    con.executemany(f'INSERT INTO "{SHEET}" VALUES (?, ?)',
                    [(i + 1, v) for i, v in enumerate(values)])
    rows = con.execute(rc._BUILDERS[template](SHEET, params)).fetchall()
    return {r[0]: r[3] for r in rows}


def test_max_limit_widened_by_margin():
    got = _flags("max_limit", {"field": "Gross Premium", "max": 1000, "tolerance_pct": 5},
                 ["1049", "1050", "1051"])
    assert set(got) == {3}
    assert got[3] == "Gross Premium exceeds maximum 1000 (margin 5%)"


def test_min_limit_widened_by_margin():
    got = _flags("min_limit", {"field": "Gross Premium", "min": 500, "tolerance_pct": 5},
                 ["476", "475", "474"])
    assert set(got) == {3}
    assert got[3].endswith("below minimum 500 (margin 5%)")


def test_range_widened_both_ways_and_exact_form():
    got = _flags("range_check", {"field": "Gross Premium", "min": 100, "max": 200,
                                 "tolerance_pct": 10}, ["90", "89", "220", "221"])
    assert set(got) == {2, 4}
    got = _flags("range_check", {"field": "Commission", "min": 0.235, "max": 0.235,
                                 "tolerance_pct": 2}, ["0.235", "0.239", "0.2405", "0.23"])
    assert set(got) == {3, 4}                # |x - 0.235| > 0.0047
    assert got[3] == "Commission must equal 0.235 (margin 2%)"
    # 0 stays exact
    assert set(_flags("range_check", {"field": "Commission", "min": 0.235, "max": 0.235},
                      ["0.235", "0.239"])) == {2}


def test_negative_limit_is_rejected():
    with pytest.raises(rc.CompileError):
        rc._b_max_limit(SHEET, {"field": "Gross Premium", "max": 1, "tolerance_pct": -1})


# ── A.3: effective tolerance ────────────────────────────────────────────────

def test_effective_tolerance_resolution():
    formula = {"kind": "ir_v1", "ir": {"template": "cross_field_math",
                                       "params": {"tolerance_pct": 1.0}}}
    own = {**formula, "tolerance_source": "rule",
           "ir": {"template": "cross_field_math", "params": {"tolerance_pct": 2.5}}}
    follows = {**formula, "tolerance_source": "contract"}
    limit = {"ir": {"template": "min_limit", "params": {"field": "x", "min": 1}}}
    # no own value → the contract margin, even over a generator-stamped 1%
    assert cm.effective_tolerance(formula, 3.0, cm.has_own_tolerance(formula, 1)) == (3.0, "contract")
    assert cm.effective_tolerance(formula, 0.0, False) == (0.0, "contract")
    # own value kept whatever the margin
    assert cm.has_own_tolerance(own, 1)
    assert cm.effective_tolerance(own, 9.0, True) == (2.5, "rule")
    # edited before tolerance_source existed → the audit event makes it own
    assert cm.has_own_tolerance(formula, 41, {41})
    assert not cm.has_own_tolerance(formula, 42, {41})
    assert cm.effective_tolerance(formula, 0.0, True) == (1.0, "rule")
    # "Use contract margin" beats an old audit event
    assert not cm.has_own_tolerance(follows, 41, {41})
    # a limit with no value of its own follows the margin; absent compiles as 0
    assert cm.effective_tolerance(limit, 4.0, False) == (4.0, "contract")
    assert cm.compiled_tolerance("min_limit", {}) == 0.0
    assert cm.compiled_tolerance("cross_field_math", {}) == 1.0
    assert cm.contract_margin_pct({"settings": {"margin_pct": "2.5"}}) == 2.5
    assert cm.contract_margin_pct({"margin_pct": 9}) == 0.0          # top level is not it
    assert cm.contract_margin_pct(None) == 0.0
    assert cm.contract_margin_pct({"settings": {"margin_pct": -1}}) == 0.0


# ── A.3/A.4: formula rules now follow the contract margin ─────────────────

def _formula_rows():
    # Net = Gross - Commission; row 1 exact, row 2 off by 0.5%, row 3 off by 1 cent
    return [_row("P-1", "New", "1000", net="900", comm="100"),
            _row("P-2", "New", "1000", net="904.5", comm="100"),
            _row("P-3", "New", "1000", net="900.01", comm="100")]


def _formula_rule(rule_id=11, **params):
    return _rule(rule_id, "cross_field_math",
                 {"result_field": "Net Premium", "left_field": "Gross Premium",
                  "operator": "-", "right_field": "Commission", **params})


def test_formula_without_settings_is_unchanged():
    rows = {e["row"] for e in _run(_formula_rows(), [_formula_rule()])}
    assert rows == set()                      # the server 1% band, as before


def test_formula_follows_contract_margin():
    ex = _run(_formula_rows(), [_formula_rule()], _settings({7: 0.0}, policy=False))
    assert {e["row"] for e in ex} == {2}     # exact to the cent; 1-cent floor holds
    ex = _run(_formula_rows(), [_formula_rule()], _settings({7: 1.0}, policy=False))
    assert ex == []


def test_formula_own_value_ignores_the_margin():
    rule = _formula_rule(tolerance_pct=2.0)
    rule["rule_spec"]["tolerance_source"] = "rule"
    assert _run(_formula_rows(), [rule], _settings({7: 0.0}, policy=False)) == []
    # the same rule edited before tolerance_source existed (legacy audit event)
    legacy = _formula_rule(tolerance_pct=2.0)
    assert _run(_formula_rows(), [legacy], _settings({7: 0.0}, own=[11], policy=False)) == []
    assert {e["row"] for e in _run(_formula_rows(), [legacy],
                                   _settings({7: 0.0}, policy=False))} == {2}


def test_limit_follows_contract_margin_per_contract():
    rows = [_row("P-1", "New", "460"), _row("P-2", "New", "440")]
    r7 = _rule(21, "min_limit", {"field": "Gross Premium", "min": 500}, terms_key="min_premium")
    r8 = _rule(22, "min_limit", {"field": "Gross Premium", "min": 500}, contract_id=8)
    ex = _run(rows, [r7, r8], _settings({7: 10.0, 8: 0.0}, policy=False))
    by_rule = {}
    for e in ex:
        by_rule.setdefault(e["rule_id"], set()).add(e["row"])
    assert by_rule == {21: {2}, 22: {1, 2}}
    assert any("(margin 10%)" in e["reason"] for e in ex if e["rule_id"] == 21)


# ── B: policy totals ────────────────────────────────────────────────────────

def _policy_rows():
    return [
        _row("P-100", "New", "1000"),
        _row("P-100", "Endorsement", "200"),
        _row("P-200", "New", "300"),
        _row("P-200", "Endorsement", "100"),
        _row("P-300", "New", "300"),
        _row("", "New", "100"),
    ]


def test_min_premium_on_policy_totals():
    rule = _rule(31, "min_limit", {"field": "Gross Premium", "min": 500}, terms_key="min_premium")
    before = _run(_policy_rows(), [rule])
    assert {e["row"] for e in before} == {2, 3, 4, 5, 6}      # today: per row
    ex = _run(_policy_rows(), [rule], _settings())
    got = {e["row"]: e for e in ex}
    assert set(got) == {3, 5, 6}
    assert got[3]["actual_value"] in ("400.0", "400")
    assert "policy total below minimum 500" in got[3]["reason"]
    assert got[3]["aggregate"] == {"level": "group", "function": "sum",
                                   "group_by": ["Policy Number"]}
    # single-row and unnumbered policies: exactly today's per-row exception
    for r in (5, 6):
        assert "aggregate" not in got[r]
        assert got[r]["reason"] == "Gross Premium below minimum 500"
        assert got[r]["reason"] == next(e["reason"] for e in before if e["row"] == r)


def test_max_on_policy_totals():
    rows = [_row("P-1", "New", "600"), _row("P-1", "Endorsement", "600"),
            _row("P-2", "New", "900")]
    rule = _rule(32, "max_limit", {"field": "Gross Premium", "max": 1000})
    ex = _run(rows, [rule], _settings())
    assert [(e["row"], e["actual_value"]) for e in ex] in ([(1, "1200.0")], [(1, "1200")])
    assert _run(rows, [rule]) == []          # per row, nothing exceeded


def test_range_on_policy_totals_and_margin_combined():
    rows = [_row("A", "New", "300"), _row("A", "End", "300"),
            _row("B", "New", "700"), _row("B", "End", "700"),
            _row("C", "New", "1050")]
    rule = _rule(33, "range_check", {"field": "Gross Premium", "min": 500, "max": 1000})
    ex = _run(rows, [rule], _settings())
    assert {e["row"] for e in ex} == {3, 5}
    assert "policy total outside [500, 1000]" in next(e for e in ex if e["row"] == 3)["reason"]
    # 10% margin: B's 1,400 still fails, C's 1,050 now passes
    ex = _run(rows, [rule], _settings({7: 10.0}))
    assert {e["row"] for e in ex} == {3}
    assert next(e for e in ex)["reason"].endswith("(margin 10%)")


def test_policy_totals_respect_scope():
    rows = [_row("P-4", "New", "300", cov="CGL"), _row("P-4", "End", "400", cov="Property"),
            _row("P-5", "New", "300", cov="CGL"), _row("P-5", "End", "300", cov="CGL")]
    rule = _rule(34, "min_limit", {"field": "Gross Premium", "min": 500,
                                   "scope": {"Coverage": "CGL"}})
    ex = _run(rows, [rule], _settings())
    # P-4 has ONE in-scope row → today's per-row check; P-5's CGL total 600 passes
    assert [(e["row"], "aggregate" in e) for e in ex] == [(1, False)]


def test_policy_totals_only_for_summing_amounts():
    rows = [_row("P-1", "New", "100", si="400"), _row("P-1", "End", "100", si="400")]
    si = _rule(35, "min_limit", {"field": "Sum Insured", "min": 500})
    si_term = _rule(36, "max_limit", {"field": "Gross Premium", "max": 150},
                    terms_key="max_sum_insured")
    ex = _run(rows, [si, si_term], _settings())
    assert sorted((e["rule_id"], e["row"]) for e in ex) == [(35, 1), (35, 2)]
    assert not cm.policy_total_eligible({"ir": {"template": "range_check", "params": {
        "field": "Gross Premium", "min": 5, "max": 5}}})
    assert not cm.policy_total_eligible({"ir": {"template": "max_limit", "params": {
        "field": "Commission Rate", "max": 5}}})
    assert cm.policy_total_eligible({"ir": {"template": "max_limit", "params": {
        "field": "Broker Fee", "max": 5}}})


def test_no_repeated_policy_means_no_change():
    rows = [_row("P-1", "New", "100"), _row("P-2", "New", "900")]
    rule = _rule(37, "min_limit", {"field": "Gross Premium", "min": 500})
    con = duckdb.connect(":memory:")
    con.execute(f'CREATE TABLE "{SHEET}" (__rowid INTEGER, "Policy Number" VARCHAR)')
    con.executemany(f'INSERT INTO "{SHEET}" VALUES (?, ?)', [(1, "P-1"), (2, "P-2")])
    tables = {SHEET: {"columns": ["Policy Number"], "filled": None}}
    out = cm.apply_rule_settings(con, tables, [rule], _settings())
    assert out[0] is rule                    # untouched — the stored query runs
    assert {e["row"] for e in _run(rows, [rule], _settings())} == {1}


# ── A.4: fail-safe recompile ────────────────────────────────────────────────

def test_recompile_failsafe_keeps_the_stored_query():
    base = _rule(41, "cross_field_math", {"result_field": "Net Premium",
                                          "left_field": "Gross Premium", "operator": "-",
                                          "right_field": "Commission"})
    stored = base["rule_spec"]["compiled_sql"]
    # 1. a recompile that raises (field gone from the IR) keeps the stored SQL
    broken = {**base, "rule_spec": {**base["rule_spec"], "ir": {
        "template": "cross_field_math", "params": {"operator": "-"}}}}
    out = cm.with_rule_settings(broken, margins={7: 0.0}, own_ids=set(), policy_keys={})
    assert out is broken
    # 2. a stored query reading TWO sheets, one spelt by an alias the plain
    #    recompile cannot reproduce → a column the stored query never read → kept
    two = stored + "\nUNION ALL\n" + stored.replace(f'"{SHEET}"', '"Other"').replace(
        '"Net Premium"', '"NET_PREM"')
    aliased = {**base, "rule_spec": {**base["rule_spec"], "compiled_sql": two}}
    out = cm.with_rule_settings(aliased, margins={7: 0.0}, own_ids=set(), policy_keys={})
    assert out is aliased
    # 3. the sound case recompiles into a COPY; the stored rule is untouched
    out = cm.with_rule_settings(base, margins={7: 0.0}, own_ids=set(), policy_keys={})
    assert out is not base and out["rule_spec"]["compiled_sql"] != stored
    assert base["rule_spec"]["compiled_sql"] == stored
    assert out["rule_spec"]["ir"]["params"]["tolerance_pct"] == 0.0
    # 4. feature off (no margins, no policy keys) → the rule itself
    assert cm.with_rule_settings(base, margins=None, own_ids=set(), policy_keys={}) is base


def test_fewer_sheets_after_recompile_keeps_stored(monkeypatch):
    base = _rule(42, "min_limit", {"field": "Gross Premium", "min": 500})
    stored = base["rule_spec"]["compiled_sql"]
    two = stored + "\nUNION ALL\n" + stored.replace(f'"{SHEET}"', '"Second"')
    rule = {**base, "rule_spec": {**base["rule_spec"], "compiled_sql": two}}
    monkeypatch.setattr(rc, "recompile_ir_for_sheets",
                        lambda ir, sheets: rc.compile_ir(ir, {"Gross Premium": [SHEET]}, SHEET))
    out = cm.with_rule_settings(rule, margins={7: 5.0}, own_ids=set(), policy_keys={})
    assert out is rule


# ── A.5: the tolerance edit on limits, contract terms and "follow contract" ──

class _Schema:
    field_names = COLS
    field_to_sheets = {c: [SHEET] for c in COLS}
    primary_sheet = SHEET
    field_aliases = None


def test_patch_tolerance_limits_terms_and_follow_contract():
    from contract_upload_services.rule_editor import patch_tolerance, RuleEditError
    term = _rule(51, "min_limit", {"field": "Gross Premium", "min": 500},
                 terms_key="min_premium")["rule_spec"]
    spec, applied = patch_tolerance(term, _Schema(), {"tolerance_pct": 5})
    assert applied == {"tolerance_pct": 5.0}
    assert spec["tolerance_source"] == "rule" and spec["source"] == "contract_terms"
    assert spec["params"]["tolerance_pct"] == 5.0 == spec["ir"]["params"]["tolerance_pct"]
    assert "(margin 5%)" in spec["compiled_sql"]
    with pytest.raises(RuleEditError):
        patch_tolerance(term, _Schema(), {"reject_pct": 9})      # limits: no band
    back, _ = patch_tolerance(spec, _Schema(), {}, follow_contract=True)
    assert back["tolerance_source"] == "contract"
    assert "tolerance_pct" not in back["ir"]["params"]
    assert back["compiled_sql"] == term["compiled_sql"]
    # formula rules keep the two-band edit and are marked own
    f = _formula_rule()["rule_spec"]
    spec, _ = patch_tolerance(f, _Schema(), {"tolerance_pct": 1, "reject_pct": 3})
    assert spec["tolerance_source"] == "rule" and spec["executable"] is True
    # anything else is still refused
    enum = {"kind": "ir_v1", "ir": {"template": "value_in_set",
                                    "params": {"field": "Coverage", "allowed": ["CGL"]}}}
    with pytest.raises(RuleEditError):
        patch_tolerance(enum, _Schema(), {"tolerance_pct": 1})


# ── the policy-number column exceptions are labelled (and grouped) by ───────

def test_policy_column_prefers_the_templates_own_policy_number():
    """Lloyd's v5.2 (the user's setup 215): "Policy or Group Ref" is mapped to
    policy_number but its name carries "group"; it must still beat a
    "Transaction number" that merely LOOKS like one (0.40)."""
    col = lambda name, conf=None, canon=None: {
        "column_name": name, "canonical_field": canon,
        "candidates": [{"canonical": "policy_number", "confidence": conf}] if conf else []}
    lloyds = {"sheets": [{"sheet_name": "Lloyds v5.2 US - Risk Mahi-demo ", "columns": [
        col("Certificate Ref"), col("Agreement No", 0.35, "contract_name"),
        col("Policy or Group Ref", 0.95, "policy_number"),
        col("Transaction number", 0.4, "premium_transaction_external_id")]}]}
    assert dv._policy_number_columns(lloyds) == {
        "Lloyds v5.2 US - Risk Mahi-demo": "Policy or Group Ref"}
    # neither mapped: the primary name still beats an expiring one
    plain = {"sheets": [{"sheet_name": "Risk", "columns": [
        col("Expiring Policy Number", 0.95), col("Policy Number", 0.9)]}]}
    assert dv._policy_number_columns(plain) == {"Risk": "Policy Number"}
    import export_results as er
    assert er.policy_column(["Certificate Ref", "Policy or Group Ref"]) == "Policy or Group Ref"
    assert er.policy_column(["Insured", "Certificate Ref"]) == "Certificate Ref"
