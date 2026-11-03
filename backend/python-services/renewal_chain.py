"""
Renewal chains: which contract in a chain a file belongs to, and re-dating a
copied rule for its new period.

A renewal is a NEW contract row pointing back at the one it renews
(contract.renews_contract_id). Once it is in force both rows stay live: the
renewal for its own period, the superseded one for late files of the old
period. Three places need to tell them apart:

  * intake — a file naming the OLD contract's code (brokers keep their habits)
    for a month the renewal covers goes to the renewal; anything unclear is
    held for a person, never guessed (route_by_period);
  * the run — a setup pins contracts by id, and those pins name the contract
    the setup was built on; when a file names another contract of the same
    chain, that one stands in for the pin (follow_pins);
  * "renew on the same wording" — copied rules hold the old term's dates in
    their settings, compiled query, name and message (redate_rule).

Periods are read the way insurance terms run: from the inception date up to,
but not including, the expiry date — so a term ending 1 Oct 2026 and its
renewal starting 1 Oct 2026 do not both claim October.

RENEWAL_CHAIN_ROUTING=false switches the intake and run behaviour off (the
old behaviour returns exactly); re-dating is part of the copy and has no switch.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from typing import Iterable, Optional


def routing_enabled() -> bool:
    return os.getenv("RENEWAL_CHAIN_ROUTING", "true").strip().lower() not in (
        "0", "false", "no", "off")


# ── chains ──────────────────────────────────────────────────────────────────

def chain_of(rows: Iterable) -> dict[int, int]:
    """{contract id: chain key} for the given rows (each with .id and
    .renews_contract_id), linking a row to the one it renews when both are in
    the set. Rows outside any chain map to themselves."""
    rows = list(rows)
    parent = {r.id: r.id for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in rows:
        prev = getattr(r, "renews_contract_id", None)
        if prev in parent:
            parent[find(r.id)] = find(prev)
    return {r.id: find(r.id) for r in rows}


def covers(c, day: dt.date) -> bool:
    """Is `day` inside the contract's term — inception inclusive, expiry
    exclusive. A missing end means open; a missing start means unknown (no)."""
    start = getattr(c, "inception_dt", None)
    end = getattr(c, "expiry_dt", None)
    if start is None:
        return False
    return start <= day and (end is None or day < end)


_MONTH = re.compile(r"^\s*(\d{4})-(\d{1,2})(?:-(\d{1,2}))?\s*$")


def period_start(label: Optional[str]) -> Optional[dt.date]:
    """First day of a reporting period label ("2026-10" or "2026-10-01").
    Anything else (a quarter, free text) is not read: None."""
    m = _MONTH.match(label or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    except ValueError:
        return None


def route_by_period(members: list, named_id: Optional[int],
                    label: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Pick the chain member for a reporting period.

    `members` are the LIVE contracts of one chain on this programme for this
    broker (two or more). Returns (contract id, None) when exactly one member's
    term covers the period, else (None, "Held: …") — the reason a person
    decides. `named_id` is the contract the file named, only used to say so.
    """
    day = period_start(label)
    names = ", ".join(f"{getattr(m, 'name', None) or f'contract {m.id}'}" for m in members)
    if day is None:
        return None, (f"Held: this programme's contract was renewed ({names}) and "
                      f"the period “{label or 'not given'}” does not say which term "
                      f"the file belongs to. Choose the contract and release it.")
    hits = [m for m in members if covers(m, day)]
    if len(hits) == 1:
        return hits[0].id, None
    if not hits:
        return None, (f"Held: {day:%B %Y} is outside every term of this contract "
                      f"and its renewal ({names}). Check the period, choose the "
                      f"contract and release it.")
    return None, (f"Held: {day:%B %Y} falls inside more than one term ({names}). "
                  f"Choose the contract and release it.")


def own_term(session, cid: Optional[int]):
    """The contract row when it is part of a renewal chain (it renews one, or
    one renews it), else None. A chain member's files belong to its OWN term:
    the previous term takes its late months, the renewal its new ones."""
    if not cid:
        return None
    from db import Contract
    c = session.get(Contract, cid)
    if c is None:
        return None
    if c.renews_contract_id or session.query(Contract.id).filter(
            Contract.renews_contract_id == cid).first() is not None:
        return c
    return None


def chain_start(session, c, limit: int = 50) -> Optional[dt.date]:
    """The earliest inception back along `c`'s renewal chain (c included)."""
    from db import Contract
    seen, starts, cur = set(), [], c
    while cur is not None and cur.id not in seen and len(seen) < limit:
        seen.add(cur.id)
        if getattr(cur, "inception_dt", None):
            starts.append(cur.inception_dt)
        cur = session.get(Contract, cur.renews_contract_id) if cur.renews_contract_id else None
    return min(starts) if starts else None


def chain_ids(session, cid: int, limit: int = 50) -> set[int]:
    """Every contract id in `cid`'s renewal chain (back and forward)."""
    from db import Contract
    ids = {cid}
    cur = session.get(Contract, cid)
    while cur is not None and cur.renews_contract_id and len(ids) < limit:
        if cur.renews_contract_id in ids:
            break
        ids.add(cur.renews_contract_id)
        cur = session.get(Contract, cur.renews_contract_id)
    frontier = [cid]
    while frontier and len(ids) < limit:
        nxt = [r[0] for r in session.query(Contract.id)
               .filter(Contract.renews_contract_id.in_(frontier)).all()
               if r[0] not in ids]
        ids.update(nxt)
        frontier = nxt
    return ids


def follow_pins(sheet_contracts: dict, named_id: Optional[int],
                chain: set[int]) -> dict:
    """Setup pins → the named contract where both are in the same chain.

    A setup built on last year's contract pins last year's id to its sheets.
    A file named under this year's renewal must be checked on this year's
    rules on those sheets too — not on both. A late file named under last
    year's contract keeps last year's. Pins to contracts outside the chain
    (another schedule's contract) are left exactly as they are."""
    if not named_id or not sheet_contracts or named_id not in chain:
        return sheet_contracts
    return {sheet: (named_id if cid in chain else cid)
            for sheet, cid in sheet_contracts.items()}


# ── who owns which day (D1) and when two versions conflict (D2, D8) ────────
#
# THE RULE (approved 14 Oct 2026). Every calendar day belongs to exactly one
# version of a contract. A version owns its days from inception to its LAST
# DAY, both inclusive. Its last day is its expiry date — except:
#   * HANDOVER: the next version starts ON that date → the day goes to the
#     NEW version, so the last day is the day before;
#   * TERMINATED earlier than its expiry → the termination date is its end, so
#     a successor may start on it (a handover) or later;
#   * OPEN-ENDED (no expiry) followed by a successor → it ends the day before
#     the successor starts.
# Two versions overlapping (both claiming a day) is refused; a gap is a warning
# a person must confirm.

def overlap_check_enabled() -> bool:
    """RENEWAL_OVERLAP_CHECK=false turns the refusals off (the report stays)."""
    return os.getenv("RENEWAL_OVERLAP_CHECK", "true").strip().lower() not in (
        "0", "false", "no", "off")


def term_end(v) -> Optional[dt.date]:
    """The date a version's term ends, as a successor must respect it: its
    expiry, or its termination date when it was terminated before expiry."""
    exp = getattr(v, "expiry_dt", None)
    term = getattr(v, "terminated_date", None)
    if term is not None and (exp is None or term < exp):
        return term
    return exp


def last_day(v, next_version=None) -> Optional[dt.date]:
    """The last day `v` owns (inclusive), given the version after it (if
    any). None = open-ended with nothing after it."""
    end = term_end(v)
    nxt = getattr(next_version, "inception_dt", None) if next_version is not None else None
    if end is None:
        return (nxt - dt.timedelta(days=1)) if nxt is not None else None
    if nxt is not None and nxt == end:
        return end - dt.timedelta(days=1)
    return end


def _label(v) -> str:
    return (getattr(v, "name", None) or f"contract {getattr(v, 'id', '?')}")


def _fmt(d: dt.date) -> str:
    return f"{d.day} {d:%b %Y}"


def term_conflicts(new_inception: Optional[dt.date], new_expiry: Optional[dt.date],
                   prev=None, next_version=None) -> list[dict]:
    """What is wrong with giving a version these dates, next to the version it
    renews (`prev`) and the version that renews it (`next_version`).

    Each item: {code, severity: "block" | "warn", message, days, with_id}.
    "block" is refused; "warn" needs a person's confirmation (a gap, D2).
    Pure: the same answer for the Renew form, the server and the tests.
    """
    out: list[dict] = []

    def add(code, severity, message, days=None, with_v=None, *,
            span=None, earliest=None):
        # The sentence, and the same facts as fields so a screen can lay them
        # out: the other version's name, the days in question (first, last),
        # and for an overlap with the previous version the earliest start.
        out.append({"code": code, "severity": severity, "message": message,
                    "days": days, "with_id": getattr(with_v, "id", None),
                    "with_name": _label(with_v) if with_v is not None else None,
                    "span_from": str(span[0]) if span else None,
                    "span_to": str(span[1]) if span else None,
                    "earliest_start": str(earliest) if earliest else None})

    if new_inception and new_expiry and new_expiry < new_inception:
        add("expiry_before_inception", "block",
            "The expiry date is before the inception date.")
        return out

    if prev is not None and new_inception is not None:
        end = term_end(prev)
        if end is not None:
            if new_inception < end:
                shared = (end - new_inception).days + 1
                add("overlaps_previous", "block",
                    f"Starts {_fmt(new_inception)}, but {_label(prev)} runs until "
                    f"{_fmt(end)}: both would claim {shared} day"
                    f"{'' if shared == 1 else 's'} ({_fmt(new_inception)} – {_fmt(end)}). "
                    f"Start on {_fmt(end)} (that day then passes to this one) or later.",
                    shared, prev, span=(new_inception, end), earliest=end)
            elif new_inception > end + dt.timedelta(days=1):
                gap = (new_inception - end).days - 1
                add("gap_after_previous", "warn",
                    f"{gap} day{'' if gap == 1 else 's'} between {_label(prev)} "
                    f"(ends {_fmt(end)}) and this one (starts {_fmt(new_inception)}) "
                    f"belong to no contract.", gap, prev,
                    span=(end + dt.timedelta(days=1), new_inception - dt.timedelta(days=1)))

    if next_version is not None:
        nxt_in = getattr(next_version, "inception_dt", None)
        draft = (getattr(next_version, "lifecycle", None) or "") in (
            "draft", "pending", "changes_requested")
        if nxt_in is not None and new_expiry is not None:
            if new_expiry > nxt_in:
                shared = (new_expiry - nxt_in).days + 1
                if draft:
                    add("overlaps_draft_renewal", "block",
                        f"Its draft renewal, {_label(next_version)}, starts "
                        f"{_fmt(nxt_in)}: both would claim {shared} day"
                        f"{'' if shared == 1 else 's'}. Correct the draft's dates first.",
                        shared, next_version, span=(nxt_in, new_expiry))
                else:
                    add("overlaps_next", "block",
                        f"{_label(next_version)} starts {_fmt(nxt_in)}: both would "
                        f"claim {shared} day{'' if shared == 1 else 's'}.",
                        shared, next_version, span=(nxt_in, new_expiry))
            elif nxt_in > new_expiry + dt.timedelta(days=1):
                gap = (nxt_in - new_expiry).days - 1
                add("gap_before_next", "warn",
                    f"{gap} day{'' if gap == 1 else 's'} between this one (ends "
                    f"{_fmt(new_expiry)}) and {_label(next_version)} (starts "
                    f"{_fmt(nxt_in)}) belong to no contract.", gap, next_version,
                    span=(new_expiry + dt.timedelta(days=1), nxt_in - dt.timedelta(days=1)))
        if (nxt_in is not None and new_inception is not None
                and new_inception >= nxt_in):
            add("no_days", "block",
                f"It would own no days: {_label(next_version)} starts "
                f"{_fmt(nxt_in)}, on or before this one's start.", None, next_version)
    return out


def blocking(conflicts: list[dict]) -> list[dict]:
    return [c for c in conflicts if c["severity"] == "block"]


def warnings(conflicts: list[dict]) -> list[dict]:
    return [c for c in conflicts if c["severity"] == "warn"]


def suggested_inception(prev) -> Optional[dt.date]:
    """Where a renewal of `prev` should start by default: on its end date when
    the term is written "1 Oct – 1 Oct" (a handover), else the day after
    ("1 Jan – 31 Dec"). Either is allowed; this is only the starting value."""
    end = term_end(prev)
    if end is None:
        return None
    start = getattr(prev, "inception_dt", None)
    if (start is not None and end.day == start.day
            and getattr(prev, "terminated_date", None) is None):
        return end
    return end + dt.timedelta(days=1)


# ── re-dating a copied rule ────────────────────────────────────────────────

_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _prose_forms(d: dt.date) -> list[str]:
    """The ways a date is written in rule names and messages. Month NAMES
    only: 01/09/2025 is the 1st of September or the 9th of January depending
    on who wrote it, and an unrelated date must never move."""
    month, mon = d.strftime("%B"), d.strftime("%b")
    return [
        f"{month} {d.day}, {d.year}", f"{month} {d.day} {d.year}",
        f"{d.day} {month} {d.year}", f"{d.day} {month}, {d.year}",
        f"{mon} {d.day}, {d.year}", f"{d.day} {mon} {d.year}",
    ]


def _one_pass(text: str, pairs: dict[str, str], whole: bool) -> str:
    """Replace every key with its value in ONE pass. A renewal usually starts on
    the old expiry date, so old-start → new-start and old-end → new-end share a
    date; replacing one after the other would move that date twice."""
    if not pairs:
        return text
    alt = "|".join(re.escape(k) for k in sorted(pairs, key=len, reverse=True))
    pat = rf"(?<![\w])(?:{alt})(?![\w])" if whole else f"(?:{alt})"
    return re.sub(pat, lambda m: pairs[m.group(0)], text)


def _pairs(moves: dict[dt.date, dt.date], prose: bool) -> dict[str, str]:
    out = {}
    for old, new in moves.items():
        out[old.isoformat()] = new.isoformat()
        if prose:
            out.update(zip(_prose_forms(old), _prose_forms(new)))
    return out


def _swap_text(text: Optional[str], moves: dict[dt.date, dt.date]) -> Optional[str]:
    # Whole dates only: "1 September 2025" must not match inside
    # "21 September 2025".
    if not text:
        return text
    return _one_pass(text, _pairs(moves, prose=True), whole=True)


def _swap_json(value, moves: dict[dt.date, dt.date]):
    if isinstance(value, dict):
        return {k: _swap_json(v, moves) for k, v in value.items()}
    if isinstance(value, list):
        return [_swap_json(v, moves) for v in value]
    if isinstance(value, str):
        # Compiled SQL and params: ISO dates, matched whole.
        return _one_pass(value, _pairs(moves, prose=False), whole=True)
    return value


def term_moves(old_in, old_ex, new_in, new_ex) -> dict[dt.date, dt.date]:
    """Old term date → new term date, only where both are known and differ."""
    moves = {}
    for a, b in ((old_in, new_in), (old_ex, new_ex)):
        if a and b and a != b:
            moves[a] = b
    return moves


def redate_rule(spec, name: Optional[str], message: Optional[str],
                moves: dict[dt.date, dt.date]) -> dict:
    """Re-date one copied rule.

    Only dates EXACTLY equal to the old term's inception or expiry move; every
    other date is left as written and reported in `other_dates` so a person can
    look at it. Returns {spec, name, message, changed, other_dates}.
    """
    moves_iso = {a.isoformat(): b.isoformat() for a, b in moves.items()}
    spec_in = spec if isinstance(spec, (dict, list)) else None
    found = set(_ISO.findall(str(spec_in) if spec_in is not None else "")) \
        | set(_ISO.findall(name or "")) | set(_ISO.findall(message or ""))
    other = sorted(d for d in found if d not in moves_iso and d not in moves_iso.values())
    new_spec = _swap_json(spec_in, moves) if spec_in is not None else spec
    new_name = _swap_text(name, moves)
    new_msg = _swap_text(message, moves)
    changed = (new_spec != spec_in) or (new_name != name) or (new_msg != message)
    return {"spec": new_spec, "name": new_name, "message": new_msg,
            "changed": changed, "other_dates": other}


def redate_json(value, moves: dict[dt.date, dt.date]):
    """Re-date ISO dates anywhere in a JSON value (e.g. contract.extracted):
    only the old term's exact start/end dates move."""
    return _swap_json(value, moves)
