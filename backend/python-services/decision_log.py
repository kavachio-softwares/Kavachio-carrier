"""Who decided an exception — recorded from the login, shown to whom it may be.

Two halves:

* ``record`` appends one ``exception_decision_log`` row per decision saved
  (db.ExceptionDecisionLog). The decider is the request's principal — never a
  user id the browser sends, which is what landing_correction.decided_by and
  validation_exception.resolved_by_user_id used to carry.

* ``label`` turns a decider into the words a given VIEWER may see. The carrier
  deals with the broker company and never sees the broker's own users, so a
  broker user's decision reads as the broker company to a carrier seat, and as
  the person to that broker's own seats. A carrier person's decision reads as
  the person inside the carrier and as the carrier company to a broker.
  Kavachio staff see the person.
"""
from __future__ import annotations

from typing import Optional

from auth_deps import BROKER_ROLES, Principal, normalize_role, resolve_broker_party_id
from db import AppUser, ExceptionDecisionLog, Party, Tenant


def decider(s, principal: Principal) -> dict:
    """The deciding seat, as the log stores it."""
    return {
        "decided_by_user_id": principal.user_id,
        "decided_by_role": principal.role,
        "decided_by_broker_party_id": (resolve_broker_party_id(s, principal)
                                       if principal.is_broker else None),
    }


def record(s, who: dict, *, lane: str, kind: str, tenant_id=None,
           export_id=None, landing_id=None, exception_id=None, program_id=None,
           broker_party_id=None, rule_id=None, policy_number=None, sheet=None,
           row=None, field=None, old_value=None, new_value=None,
           reason=None) -> None:
    """Append one decision. Added to the caller's session, committed with the
    decision itself, so the log and the decision can never disagree."""
    s.add(ExceptionDecisionLog(
        tenant_id=tenant_id, export_id=export_id, landing_id=landing_id,
        exception_id=exception_id, program_id=program_id,
        broker_party_id=broker_party_id, lane=lane, kind=(kind or "").lower(),
        rule_id=rule_id, policy_number=policy_number, sheet=sheet, row=row,
        field=field, old_value=old_value, new_value=new_value, reason=reason,
        **who))


class Labeller:
    """Decider -> display words for one viewer. Built once per response, so the
    viewer's broker and each person/company are looked up once."""

    def __init__(self, s, viewer: Optional[Principal]):
        self.s = s
        self.viewer = viewer
        self.viewer_broker = (resolve_broker_party_id(s, viewer)
                              if viewer is not None and viewer.is_broker else None)
        self._users: dict = {}

    def _user(self, user_id):
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return None
        if uid not in self._users:
            self._users[uid] = self.s.get(AppUser, uid)
        return self._users[uid]

    def label(self, user_id) -> Optional[str]:
        v, u = self.viewer, self._user(user_id)
        if v is None or u is None:
            return None
        person = u.full_name or u.email
        if v.is_platform_admin:
            return person
        role = normalize_role(u.role)
        if role == "kavachio_admin":
            return "Kavachio"
        if role in BROKER_ROLES:
            if v.is_broker and self.viewer_broker == u.broker_party_id:
                return person
            party = self.s.get(Party, u.broker_party_id) if u.broker_party_id else None
            return (party.legal_name if party else None) or "The broker"
        # A carrier person.
        if not v.is_broker and v.tenant_id == u.tenant_id:
            return person
        t = self.s.get(Tenant, u.tenant_id) if u.tenant_id else None
        return (getattr(t, "legal_name", None) or getattr(t, "tenant_name", None)
                or "The carrier")
