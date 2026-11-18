"""
policy_export.py
────────────────
Policy Search 43.2 and 43.3: the policy list taken out of Kavachio — as an
Excel file, or by email — with a named row in the Audit Logs each time.

WHAT GOES OUT
  Exactly the list on screen: the same search (policy_search.search) with the
  same filters and the same number clicked on a report (drill_down), every
  page of it. A "Policies" sheet in the screen's columns, and an "About this
  list" sheet saying what was picked, by whom and when.

WHO IT CAN GO TO
  As on the Executive Report's Send (executive_delivery.recipients): the
  seat's own logins ticked — the carrier's (report_delivery.people) or the
  broker company's — and any other address typed. Outside addresses still
  pass through email_utils' MAIL_ALLOWED_RECIPIENTS guard. From "<company>
  via Kavachio"; a reply goes to the person who sent it.

WHAT IS RECORDED (43.3)
  policies_exported / policies_sent in activity_events, by the acting seat:
  how many policies, the filters, and who it went to. A broker's row names
  only what the broker typed (search, state, dates) — every carrier the
  broker works for reads that broker's rows in its Audit Logs
  (audit_feed.scope_for), so a programme or carrier name there would show one
  carrier another's book.

Every function takes the caller's session; none opens one. Nothing here
commits: the route does, once the row is added.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import drill_down
import executive_delivery as ed
import policy_search as ps
import report_delivery as rd

log = logging.getLogger("policy_export")

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPORTED, SENT = "policies_exported", "policies_sent"
NO_POLICIES = "There are no policies in this list."
NO_ONE = "Choose at least one person to send the list to."

# The screen's date choices (PolicySearch.tsx DATE_FIELDS), for the file and the email.
DATE_WORDS = {"start": "Cover start", "end": "Cover end", "month": "Bordereau month"}
# The keys of a report number clicked (frontend api/policies DRILL_KEYS), for
# the email's "Open in Kavachio" link back to the same list.
_LINK_DRILL_KEYS = ("from", "label", "basis", "period", "window", "metric",
                    "currency", "open_problems", "month", "measure")


# ── words ───────────────────────────────────────────────────────────────────
def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _day_words(v: Optional[str]) -> Optional[str]:
    """'2026-01-31' → '31 Jan 2026'; anything else as written."""
    try:
        d = dt.date.fromisoformat(v) if v else None
    except ValueError:
        return v
    return f"{d.day} {d:%b %Y}" if d else None


def _when(at: dt.datetime) -> str:
    return f"{at.day} {at:%b %Y, %H:%M} UTC"


def picked(result: dict, f: dict, *, names: bool = True) -> list[tuple[str, str]]:
    """The filters in use, in the screen's words, as (label, value).

    `names` False leaves out programme, broker and carrier names, and the
    report's own label for a number clicked — what a broker's audit row must
    not carry (see WHAT IS RECORDED)."""
    out: list[tuple[str, str]] = []
    d = result.get("drill")
    if d:
        what = (d.get("label") if names else "a number on a report") or "a number on a report"
        out.append(("Behind", " · ".join(x for x in (what, d.get("period_name")) if x)))
    if f.get("q"):
        out.append(("Search", f["q"]))
    if names:
        def name_of(key: str, i: int) -> str:
            return next((c["name"] for c in result["filters"].get(key, []) if c["id"] == i), f"#{i}")
        if f.get("program_id") is not None:
            out.append(("Programme", name_of("programmes", f["program_id"])))
        if result["seat"] == "carrier" and f.get("broker_party_id") is not None:
            out.append(("Broker", name_of("brokers", f["broker_party_id"])))
        if result["seat"] == "broker" and f.get("carrier_id") is not None:
            out.append(("Carrier", name_of("insurers", f["carrier_id"])))
    if f.get("state"):
        out.append(("State", f["state"]))
    if f.get("date_from") or f.get("date_to"):
        lo, hi = _day_words(f.get("date_from")), _day_words(f.get("date_to"))
        span = f"{lo} – {hi}" if lo and hi else f"from {lo}" if lo else f"up to {hi}"
        out.append((DATE_WORDS.get(f.get("date_field") or "start", "Date"), span))
    return out


def org_name(s, seat: ps.Seat) -> str:
    """The company taking the list out: the carrier, or the broker company."""
    if seat.kind == "carrier":
        return rd.carrier_name(s, seat.tenant_id) or ""
    from db import Party
    row = s.get(Party, seat.broker_party_id) if seat.broker_party_id else None
    return ((row.legal_name if row else None) or "").strip()


def filename(at: dt.datetime) -> str:
    return f"Policies {at:%Y-%m-%d}.xlsx"


# ── the file ────────────────────────────────────────────────────────────────
def _date_cell(v: Any):
    """A real date when the file gave one (policy_search writes YYYY-MM-DD),
    so Excel sorts and filters it; otherwise the file's own text."""
    try:
        return dt.date.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return v


def _month_cell(v: Optional[str]) -> Optional[str]:
    return drill_down.period_name(v) if v else None


def columns(seat_kind: str) -> list[tuple[str, int, Callable[[dict], Any]]]:
    """The screen's columns, with what it squeezes into one cell split out:
    cover start and end, premium and its currency, first and latest bordereau.
    A carrier's list names the broker; a broker's, the carrier."""
    party = (("Broker", 26, lambda p: p["broker"] or "No broker") if seat_kind == "carrier"
             else ("Carrier", 26, lambda p: p["insurer"]))
    return [
        ("Policy number", 20, lambda p: p["policy"]),
        ("Insured", 32, lambda p: p["insured"]),
        ("Programme", 26, lambda p: p["programme"]),
        party,
        ("State", 10, lambda p: p["state"]),
        ("Cover start", 13, lambda p: _date_cell(p["start"])),
        ("Cover end", 13, lambda p: _date_cell(p["end"])),
        ("Latest transaction", 18, lambda p: p["transaction"]),
        ("Premium", 15, lambda p: p["premium"]),
        ("Currency", 10, lambda p: p["currency"]),
        ("Latest bordereau", 17, lambda p: _month_cell(p["last_period"])),
        ("First bordereau", 17, lambda p: _month_cell(p["first_period"])),
        ("Bordereaux", 11, lambda p: p["files"]),
        ("Rows", 8, lambda p: p["rows"]),
    ]


_NUMBER_FORMATS = {"Cover start": "d mmm yyyy", "Cover end": "d mmm yyyy", "Premium": "#,##0.00"}


def workbook(result: dict, f: dict, *, org: str, by: Optional[str], at: dt.datetime) -> bytes:
    """Every policy of `result` (a search with page_size None) as an Excel
    file, in the look of the app's other list downloads (Partner Performance,
    Audit Logs): a dark header row, frozen, with filters on every column."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    cols = columns(result["seat"])
    wb = Workbook()
    ws = wb.active
    ws.title = "Policies"
    ws.append([c[0] for c in cols])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for p in result["items"]:
        ws.append([c[2](p) for c in cols])
    for i, (label, width, _f) in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
        fmt = _NUMBER_FORMATS.get(label)
        if fmt:
            for (cell,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                cell.number_format = fmt
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions

    info = wb.create_sheet("About this list")
    chosen = picked(result, f) or [("Filters", "None — every policy")]
    for label, value in [("Policies", _plural(result["total"], "policy", "policies")),
                         ("From", org or "—"),
                         ("Taken out by", by or "—"),
                         ("On", _when(at)),
                         ("Read from", _plural(result["files"], "processed bordereau",
                                               "processed bordereaux")
                          + ", each as its latest version")] + chosen:
        info.append([label, value])
    for (cell,) in info.iter_rows(min_col=1, max_col=1):
        cell.font = Font(bold=True)
    info.column_dimensions["A"].width = 18
    info.column_dimensions["B"].width = 70
    # 45.2 — what every column means, in plain English.
    import export_glossary as gl
    gl.xlsx_sheet(wb, "policies")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ── who it can go to ────────────────────────────────────────────────────────
def logins(s, seat: ps.Seat) -> list[dict]:
    """The seat's own logins that can be ticked: the carrier's active logins
    (report_delivery.people, as on the reports), or the broker company's."""
    if seat.kind == "carrier":
        return [{"user_id": p["user_id"], "email": p["email"], "name": p["name"]}
                for p in rd.people(s, seat.tenant_id)]
    from sqlalchemy import func
    from auth_deps import db_role_values
    from db import AppUser
    rows = (s.query(AppUser.id, AppUser.email, AppUser.full_name)
            .filter(AppUser.broker_party_id == seat.broker_party_id,
                    AppUser.role.in_(db_role_values("broker_admin")),
                    func.lower(func.coalesce(AppUser.status, "active")) == "active")
            .order_by(AppUser.full_name).all())
    return [{"user_id": i, "email": e.strip(), "name": (n or "").strip() or None}
            for i, e, n in rows if (e or "").strip()]


# ── the record (43.3) ───────────────────────────────────────────────────────
def record(s, principal, seat: ps.Seat, result: dict, f: dict, action: str, *,
           file: str, extra: Optional[dict] = None) -> None:
    """One named Audit Logs row: who took how many policies out, with which
    filters, and (for an email) to whom. Flat details, so the Audit Logs line
    reads them (audit_feed._detail_words skips nested ones). Caller commits."""
    from audit import actor_columns, actor_for
    from db import ActivityEvent
    carrier = seat.kind == "carrier"
    details: dict[str, Any] = {"policies": result["total"],
                               **{k: v for k, v in (extra or {}).items() if v is not None}}
    for label, value in picked(result, f, names=carrier):
        details[label.lower()] = value
    details["filename"] = file
    s.add(ActivityEvent(tenant_id=seat.tenant_id if carrier else f.get("carrier_id"),
                        actor=actor_for(principal), action=action, target="policies",
                        details=details, **actor_columns(principal)))


# ── the download ────────────────────────────────────────────────────────────
def export(s, principal, result: dict, f: dict,
           now: Optional[dt.datetime] = None) -> tuple[str, bytes]:
    """The Excel file of `result` as (filename, bytes), and its Audit Logs
    row. ValueError when the list is empty. Caller commits."""
    if not result["total"]:
        raise ValueError(NO_POLICIES)
    now = now or dt.datetime.utcnow()
    seat = ps.seat_of(s, principal)
    name = filename(now)
    data = workbook(result, f, org=org_name(s, seat), by=ed._person(s, principal.user_id)[0], at=now)
    record(s, principal, seat, result, f, EXPORTED, file=name)
    return name, data


# ── the email ───────────────────────────────────────────────────────────────
def _link(seat: ps.Seat, f: dict) -> Optional[str]:
    """The same list in Kavachio, for the people who can sign in: the search,
    programme, broker and number clicked — what the screen reads from its address."""
    from notifications import _app_link
    q = {"q": f.get("q"), "program": f.get("program_id"),
         "broker": f.get("broker_party_id") if seat.kind == "carrier" else None,
         **{k: f.get(k) for k in _LINK_DRILL_KEYS}}
    q = {k: v for k, v in q.items() if v not in (None, "")}
    return _app_link("/policies" + (f"?{urlencode(q)}" if q else ""))


def send(s, principal, result: dict, f: dict, *, user_ids: Optional[list],
         emails: Optional[list], now: Optional[dt.datetime] = None,
         mailer: Optional[Callable[..., None]] = None) -> dict:
    """Email the list as an Excel file to the logins ticked and the addresses
    typed, and add its Audit Logs row. ValueError when there is nothing to
    send or no one to send it to. Caller commits."""
    from notifications import NOTIFY_MAIL_ACCOUNT, _email_text, notification_email_html
    if mailer is None:
        from email_utils import send_email as mailer
    if not result["total"]:
        raise ValueError(NO_POLICIES)
    seat = ps.seat_of(s, principal)
    to = ed.recipients(s, None, user_ids, emails, logins=logins(s, seat))
    if not to:
        raise ValueError(NO_ONE)
    now = now or dt.datetime.utcnow()
    org = org_name(s, seat)
    sender, reply_to = ed._person(s, principal.user_id)
    name = filename(now)
    attachments = [(name, workbook(result, f, org=org, by=sender, at=now), XLSX)]

    count = _plural(result["total"], "policy", "policies")
    title = "Policy list"
    subject = rd.SUBJECT_PREFIX + f"Policy list, {count}"
    whose = f"{org}'s policy list" if org else "A policy list"
    body = f"{whose} from Kavachio: {count}. It is attached as an Excel file: {name}."
    facts = [("Policies", count)] + picked(result, f)
    footer = (f"Sent by {sender} from {org} on Kavachio." if sender and org
              else f"Sent from {org or 'Kavachio'} on Kavachio.")
    from_name = f"{org} via Kavachio" if org else None
    link = _link(seat, f)

    results = []
    for p in to:
        # A login gets "Open in Kavachio"; an outside address has no login,
        # so it reads the list in the attachment.
        their_link = link if p.get("user_id") else None
        try:
            mailer(p["email"], subject,
                   notification_email_html(title, body, facts, their_link, "Open in Kavachio",
                                           greeting_name=p.get("name"), footer=footer,
                                           badge="Policies"),
                   text=_email_text(title, body, facts, their_link),
                   account=NOTIFY_MAIL_ACCOUNT, attachments=attachments,
                   sender_name=from_name, reply_to=reply_to)
            results.append({**p, "delivered": True})
        except Exception as e:  # noqa: BLE001 — one bad address must not stop the rest
            log.warning("policy list to %s failed: %s", p["email"], e)
            results.append({**p, "delivered": False})
    delivered = sum(1 for r in results if r["delivered"])
    failed = len(results) - delivered
    record(s, principal, seat, result, f, SENT, file=name,
           extra={"to": [r["email"] for r in results], "failed": failed or None})
    return {"policies": result["total"], "delivered": delivered, "failed": failed,
            "recipients": [{k: r[k] for k in ("name", "email", "delivered")} for r in results]}
