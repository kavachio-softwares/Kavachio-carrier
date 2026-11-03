"""Which contract rules a Bordereau Setup runs — rule_scope.py's decisions.

No database: the decisions are pure, and the lookups that feed them are
exercised by passing their results in directly.

The scenario throughout is the one this exists for. Contract 7 was bound to
template 202 and carries its rules untagged; a separate set was later added
for template 203. Template 202 also has a later version, 212, made by editing
it while in use.
"""
import rule_scope as rs

FAMILY = {202: {202, 212}, 212: {202, 212}, 203: {203}, 999: {999}}


def applies(rule_id, tag, *, setup, scope=None, contract_template=202):
    return rs.rule_applies(
        rule_id=rule_id, rule_tag=tag, contract_template=contract_template,
        setup_family=FAMILY.get(setup, {setup}), scope=rs.normalize_scope(scope),
        family_of=FAMILY)


# ── A setup that never chose: exactly what it ran before ────────────────────

def test_an_old_setup_runs_every_untagged_rule_whatever_its_template():
    # Every rule that existed before this is untagged, and every setup ran all
    # of its contract's rules. That must not change for any template.
    for setup in (202, 212, 203, 999):
        assert applies(1, None, setup=setup)


def test_a_setup_that_never_chose_does_not_pick_up_a_set_added_for_another_template():
    assert not applies(2, 203, setup=202)


def test_a_setup_runs_the_set_added_for_its_own_template():
    assert applies(2, 203, setup=203)


def test_a_set_follows_every_version_of_its_template():
    # Editing a template in use forks the next version under the same name; a
    # setup moved onto it must not silently lose the rules.
    assert applies(3, 202, setup=212)


# ── A setup that chose ──────────────────────────────────────────────────────

def test_2025_setup_keeps_old_rule_5_and_2026_setup_excludes_it():
    old_2025 = {"template_ids": [202]}
    new_2026 = {"template_ids": [202, 203], "excluded_rule_ids": [5]}
    assert applies(5, None, setup=202, scope=old_2025)
    assert not applies(5, None, setup=203, scope=new_2026)
    # The rest of the old set, and the new set, still run in 2026.
    assert applies(6, None, setup=203, scope=new_2026)
    assert applies(7, 203, setup=203, scope=new_2026)


def test_a_setup_can_run_only_the_new_set():
    only_new = {"template_ids": [203]}
    assert not applies(1, None, setup=203, scope=only_new)
    assert applies(2, 203, setup=203, scope=only_new)


def test_choosing_a_template_takes_all_its_versions():
    assert applies(3, 212, setup=203, scope={"template_ids": [202]})


def test_excluding_one_rule_without_choosing_templates_keeps_everything_else():
    scope = {"excluded_rule_ids": [5]}
    assert not applies(5, None, setup=202, scope=scope)
    assert applies(6, None, setup=202, scope=scope)
    assert not applies(7, 203, setup=202, scope=scope)


def test_a_rule_written_for_no_template_stays_with_every_setup():
    assert applies(8, None, setup=203, scope={"template_ids": [203]},
                   contract_template=None)


# ── Reading a stored or submitted scope ─────────────────────────────────────

def test_no_scope_and_empty_scope_mean_the_same_thing():
    assert rs.normalize_scope(None) is None
    assert rs.normalize_scope({}) is None
    assert rs.normalize_scope({"template_ids": None, "excluded_rule_ids": []}) is None


def test_a_malformed_scope_never_switches_checks_off():
    for bad in ("not json", 42, ["x"], {"template_ids": "202"},
                {"template_ids": [None]}, {"template_ids": ["abc"]},
                {"template_ids": [True]}):
        assert rs.normalize_scope(bad) is None, bad


def test_scope_ids_are_cleaned_and_deduplicated():
    got = rs.normalize_scope('{"template_ids": ["202", 202, "x", 203], '
                             '"excluded_rule_ids": [5, "5", null]}')
    assert got == {"template_ids": [202, 203], "excluded_rule_ids": [5]}


def test_an_explicit_empty_template_choice_is_kept():
    # Distinct from "no choice": the user unticked every set.
    assert rs.normalize_scope({"template_ids": []}) == {
        "template_ids": [], "excluded_rule_ids": []}
    assert not applies(1, None, setup=202, scope={"template_ids": []})


def test_effective_template():
    assert rs.effective_template(None, 202) == 202
    assert rs.effective_template(203, 202) == 203
    assert rs.effective_template(None, None) is None


# ── filter_rules takes no database for the case every setup is in today ──────

class _NoDatabase:
    def __getattr__(self, name):
        raise AssertionError(f"database touched: {name}")


def test_nothing_is_queried_for_a_setup_without_a_scope_and_untagged_rules():
    rules = [{"rule_id": 1, "contract_id": 7, "rule_template_id": None},
             {"rule_id": 2, "contract_id": 7, "rule_template_id": None}]
    assert rs.filter_rules(_NoDatabase(), rules, setup_template_id=202,
                           scope=None) is rules


# ── Column checks: remembered only once present ─────────────────────────────

class _Inspector:
    def __init__(self, cols):
        self.cols = cols

    def get_columns(self, table):
        return [{"name": c} for c in self.cols.get(table, [])]


def test_an_absent_column_is_asked_again_and_a_present_one_is_remembered(monkeypatch):
    import sqlalchemy
    cols = {"validation_rule": ["rule_id"]}
    calls = []

    def fake_inspect(engine):
        calls.append(engine)
        return _Inspector(cols)

    monkeypatch.setattr(sqlalchemy, "inspect", fake_inspect)
    monkeypatch.setattr(rs, "_PRESENT", set())
    monkeypatch.setattr(rs, "_ABSENT_AT", {})
    monkeypatch.setattr(rs, "_ABSENT_RECHECK_S", 0.0)
    engine = object()

    assert rs.tag_column_present(engine) is False
    cols["validation_rule"].append("output_template_id")     # migration applied
    assert rs.tag_column_present(engine) is True
    n = len(calls)
    assert rs.tag_column_present(engine) is True
    assert len(calls) == n                                   # remembered, not asked
