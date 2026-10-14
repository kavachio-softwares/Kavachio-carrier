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
