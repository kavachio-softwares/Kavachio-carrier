"""audit_one — the Area / Action filters that replaced the Audit Logs tabs.

Pure: no database. Run with DATABASE_URL=sqlite:// so importing db opens nothing shared.
"""
import audit_feed as af
import audit_one as ao


def test_every_known_action_has_an_action_type():
    assert [n for n in ao.KNOWN if ao.verb_of(n) is None] == []


def test_words_people_would_expect():
    assert ao.verb_of("contract_signed_from_email") == "signed"
    assert ao.verb_of("treaty_approved_by_reinsurer") == "approved"
    assert ao.verb_of("treaty_changes_requested_by_reinsurer") == "asked"
    assert ao.verb_of("contract_sent_for_review") == "sent"        # not "viewed"
    assert ao.verb_of("rule.disabled") == "changed"                # switched off, not deleted
    assert ao.verb_of("setup_made_active_again") == "restored"
    assert ao.verb_of("login_success") == "signin"
    assert ao.verb_of("intake_sftp_login_issued") == "added"
    assert ao.verb_of("download") == "downloaded"
    assert ao.verb_of("DELETE /rule-library/{id}") == "removed"
    assert ao.verb_of("POST /broker/calendar/{id}/dispute") == "asked"


def test_areas():
    assert ao.area_key_of("reinsurance_setup_deleted") == "reinsurance_setup"     # not in any list
    assert ao.area_key_of("reinsurance_bordereau_sent") == "reinsurance_bdx"
    assert ao.area_key_of("treaty_signed_by_reinsurer") == "signing"             # signing covers both kinds
    assert ao.area_key_of("rule_updated") == "rule_library"
    assert ao.area_key_of("rule.disabled") == "rules"
    assert ao.area_key_of("contract_raised") == "contracts"
    assert ao.area_key_of("submission_overdue") == "calendar"
    assert ao.area_key_of("policies_exported") == "policies"
    assert ao.area_key_of("POST /reinsurance/setups/{id}/layout/upload") == "reinsurance_setup"
    assert ao.area_key_of("PUT /pipelines/{id}") == "setup"


def test_no_area_or_action_leaves_the_filters_alone():
    f = af.Filters()
    assert ao.narrow(None, f, "", "") is f
    assert f.actions == () and f.categories == af.CATEGORIES


def test_area_and_action_together_narrow_to_both(monkeypatch):
    monkeypatch.setattr(ao, "_stored_names", lambda s: ())
    f = ao.narrow(None, af.Filters(), "contracts", "added")
    assert "contract_raised" in f.actions
    assert "contract_signed" not in f.actions and "contract_accepted" not in f.actions
    assert f.categories == ("activity",)


def test_an_area_never_brings_back_hidden_request_copies(monkeypatch):
    monkeypatch.setattr(ao, "_stored_names", lambda s: ("POST /contract-wording/preview",))
    f = ao.narrow(None, af.Filters(), "contracts", "")
    assert not set(f.actions) & set(af.SUPPRESSED_ACTIONS)


def test_nothing_in_common_reads_nothing(monkeypatch):
    monkeypatch.setattr(ao, "_stored_names", lambda s: ())
    f = ao.narrow(None, af.Filters(), "auth", "signed")
    assert f.actions == ("__nothing__",)


def test_downloads_area_includes_views(monkeypatch):
    monkeypatch.setattr(ao, "_stored_names", lambda s: ())
    f = ao.narrow(None, af.Filters(), "m_access", "")
    assert f.views is True and "access" in f.categories and f.access_kind == ""
    f = ao.narrow(None, af.Filters(), "views", "")
    assert f.views is True and f.access_kind == "view" and "download" in f.actions


def _subs(seat):
    class V:
        pass
    V.seat = seat
    o = ao.options(None, V())
    return {m["key"]: [x["key"] for x in m["subs"]] for m in o["modules"]}


def test_seats_only_see_their_areas():
    mods = _subs("broker_admin")
    subs = {x for xs in mods.values() for x in xs}
    assert "contracts" in subs and "files" in subs
    assert "treaties" not in subs and "setup" not in subs and "m_reinsurance" not in mods


def test_areas_follow_the_menu():
    mods = _subs("carrier_admin")
    assert list(mods) == [k for k, _l, _s in ao.MODULES]
    assert mods["m_contracts"] == ["contracts", "treaties", "signing", "rules"]
    assert mods["m_reinsurance"] == ["reinsurance_setup", "reinsurance_bdx"]
    assert not {"audits", "integrity", "sessions"} & {x for xs in mods.values() for x in xs}


def test_every_sub_area_is_explained_and_in_one_area():
    offered = [x for _k, _l, subs in ao.MODULES for x in subs]
    assert len(offered) == len(set(offered))
    assert set(offered) == {k for k, _l, _a, _s in ao.AREAS} - ao.NOT_OFFERED
    assert set(offered) <= set(ao.AREA_HELP)


def test_an_area_is_all_its_sub_areas(monkeypatch):
    monkeypatch.setattr(ao, "_stored_names", lambda s: ())
    f = ao.narrow(None, af.Filters(), "m_contracts", "")
    assert {"contract_raised", "treaty_sent_for_approval", "contract_signed"} <= set(f.actions)
    f = ao.narrow(None, af.Filters(), "treaties", "")
    assert "treaty_sent_for_approval" in f.actions and "contract_raised" not in f.actions


def test_an_areas_own_action_narrows_to_those_names(monkeypatch):
    monkeypatch.setattr(ao, "_stored_names", lambda s: ())
    f = ao.narrow(None, af.Filters(), "contracts", "contract_raised")
    assert f.actions == ("contract_raised",)
    f = ao.narrow(None, af.Filters(), "contracts", "contract_signed")     # not a contracts-area name
    assert f.actions == ("__nothing__",)


def test_each_area_lists_its_own_actions_and_statuses():
    order = list(ao.VERB_ORDER)
    for subs in (("contracts",), ("treaties",), ("intake",), ("signing",), ("auth",), ("contracts", "signing")):
        keys = [a["key"] for a in ao._area_actions(subs, set(ao.KNOWN))]
        assert keys and keys == [k for k in order if k in keys]      # same order everywhere
    contracts = {a["key"] for a in ao._area_actions(("contracts",), set(ao.KNOWN))}
    assert "signin" not in contracts and "downloaded" not in contracts
    assert {a["key"] for a in ao._area_actions(("auth",), set(ao.KNOWN))} == {"signin"}
    assert "Successful" in ao._area_statuses(("auth",))
    assert "Deleted" in ao._area_statuses(("rule_library",))
    assert ao._area_statuses(("views",)) == ["Viewed"]


def test_status_is_the_badge_words():
    assert ao._status_ok({"status": "Signed", "tone": "ok"}, "signed")
    assert not ao._status_ok({"status": "Signed", "tone": "ok"}, "Deleted")
    assert ao._status_ok({"status": "Deleted", "tone": "bad"}, "failed")      # an older link's group


def test_search_looks_at_every_column_shown():
    row = {"actor": "Dana", "actor_role": "Carrier", "actor_org": "Insurisk Specialty", "area": "Reinsurance",
           "action_label": "Activated a reinsurance bordereau setup", "detail": "Treaty QS canonical",
           "target_kind": "Contract", "target": "QS canonical · Spectrum Property", "old": "Version 4",
           "new": "Version 2", "status": "Activated", "carrier": None, "changes": [],
           "at": "2026-11-06T10:33:44Z"}
    for q in ("dana", "insurisk", "reinsurance", "activated", "spectrum", "version 4", "06 nov 2026",
              "16:03", "dana spectrum"):
        assert ao._matches(row, q, 330), q                           # 330: India, UTC+5:30
    assert not ao._matches(row, "wani", 330)
    assert not ao._matches(row, "dana wani", 330)                     # every word must be there
