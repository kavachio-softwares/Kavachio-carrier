"""Which of a contract's rules belong to which output template, and which of
them a Bordereau Setup runs.

A rule is a contract clause bound to ONE output template's columns: the same
clause against a different template names different columns, so it is a
different rule. A contract bound to two templates therefore carries two rule
sets, side by side, and neither is ever retired to make room for the other.

Two facts decide everything here:

  validation_rule.output_template_id — set ONLY on a rule set ADDED for a
      template other than the one the contract is bound to. The contract's own
      set — every rule written before this existed, and every set still written
      by upload or by a contract's first binding — stays NULL ("untagged") and
      belongs to contract.output_template_id, following it wherever an existing
      flow re-points it. Tagging only the added sets is what keeps every
      existing path writing and reading exactly what it did.

  pipeline.rule_scope — the setup's own choice, for that setup only:
      {"template_ids": [202, 203], "excluded_rule_ids": [5]}
      NULL on every setup built before this existed.

What a setup runs:

  scope NULL, or scope without template_ids
      every untagged rule (exactly what every setup ran before), plus the rules
      tagged with the setup's own template. A rule set written later for a
      DIFFERENT template never reaches a setup that did not ask for it.
  scope with template_ids
      the rules whose effective template is one of them.
  either way
      minus excluded_rule_ids.

Templates are matched by FAMILY, not by id. Editing a template that is in use
forks the next version under the same name (output_template_routes._fork),
and a setup can move to that version; matching by id would silently drop every
rule of a setup whose template was edited. Versions of one template are the
same template everywhere else too — see setup_scope.same_template.

Nothing here writes. Every reader that decides which rules RUN for a setup
asks `filter_rules`, so a run, the setup's missing-column note and the setup's
field review can never disagree about the rule set.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable, Optional

from sqlalchemy import text

# (table, column) -> True once seen. Only a column that EXISTS is remembered;
# an absent one is asked again after _ABSENT_RECHECK_S, so a process that
# started before migration 21 notices it without a restart — a reader that
# still thought the column missing while a writer already used it would treat
# an added rule set as the contract's own.
_PRESENT: set[tuple[str, str]] = set()
_ABSENT_AT: dict[tuple[str, str], float] = {}
_ABSENT_RECHECK_S = 30.0


# ---------------------------------------------------------------------------
# Pure decisions — no database. Tested directly in test_rule_scope.py.
# ---------------------------------------------------------------------------

def normalize_scope(raw: Any) -> Optional[dict]:
    """A stored or submitted rule_scope, reduced to the two lists it may carry.

    None for no scope at all. Anything unreadable is treated as no scope rather
    than as "run nothing": a malformed value must never switch a setup's checks
    off without anybody having asked for it."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None

    def _ids(v) -> Optional[list[int]]:
        if v is None:
            return None
        if not isinstance(v, (list, tuple, set)):
            return None
        out: list[int] = []
        for x in v:
            if isinstance(x, bool):
                continue
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if i not in out:
                out.append(i)
        # A list that named something, none of it an id, is unreadable — not
        # an empty choice. Read as [] it would mean "no template's rules".
        if v and not out:
            return None
        return out

    template_ids = _ids(raw.get("template_ids"))
    excluded = _ids(raw.get("excluded_rule_ids")) or []
    if template_ids is None and not excluded:
        return None
    return {"template_ids": template_ids, "excluded_rule_ids": excluded}


def effective_template(rule_tag: Optional[int],
                       contract_template: Optional[int]) -> Optional[int]:
    """The template a rule was written for: its own tag, else the template its
    contract is bound to. None only for a rule with neither."""
    return rule_tag if rule_tag is not None else contract_template


def rule_applies(*, rule_id: int, rule_tag: Optional[int],
                 contract_template: Optional[int],
                 setup_family: set[int], scope: Optional[dict],
                 family_of: dict[int, set[int]]) -> bool:
    """Does this setup run this rule?

    `setup_family` — ids of every version of the setup's own template.
    `family_of`    — template id → ids of every version of it, for each id
                     named by the scope or by a rule's effective template."""
    scope = scope or {}
    if rule_id in set(scope.get("excluded_rule_ids") or []):
        return False
    template_ids = scope.get("template_ids")
    if template_ids is None:
        # No template choice made: what every setup ran before, plus its own
        # template's newer, tagged set.
        return rule_tag is None or rule_tag in setup_family
    eff = effective_template(rule_tag, contract_template)
    if eff is None:
        # Written for no template at all — nothing to tell it apart by, so it
        # stays with every setup, as it always has.
        return True
    chosen: set[int] = set()
    for t in template_ids:
        chosen |= family_of.get(t, {t})
    return eff in chosen


# ---------------------------------------------------------------------------
# Database lookups — read only.
# ---------------------------------------------------------------------------

def _column_present(bind, table: str, column: str) -> bool:
    """Does `table.column` exist? By reflection on the ENGINE, never by a probe
    inside the caller's transaction: in Postgres a failed statement aborts the
    whole transaction and catching it does not heal it (see
    contract_asof._columns_present, which this follows). `bind` may be a
    Session, a Connection or an Engine."""
    key = (table, column)
    if key in _PRESENT:
        return True
    seen = _ABSENT_AT.get(key)
    if seen is not None and time.monotonic() - seen < _ABSENT_RECHECK_S:
        return False
    engine = bind
    if hasattr(engine, "get_bind"):
        try:
            engine = engine.get_bind()
        except Exception:  # noqa: BLE001
            pass
    engine = getattr(engine, "engine", engine)
    try:
        from sqlalchemy import inspect as _sa_inspect
        present = column in {c["name"] for c in
                             _sa_inspect(engine).get_columns(table)}
    except Exception:  # noqa: BLE001
        present = False
    if present:
        _PRESENT.add(key)
        _ABSENT_AT.pop(key, None)
    else:
        _ABSENT_AT[key] = time.monotonic()
    return present


def tag_column_present(s) -> bool:
    """Is validation_rule.output_template_id there to read and write?

    The app adds it at start-up (db.init_db) and migrations/21 adds it where
    the app cannot run DDL. Every reader AND every writer of the tag asks this,
    so on a database the migration has not reached every rule reads as the
    contract's own, no added set can be written, and everything behaves as it
    did before this module existed."""
    return _column_present(s, "validation_rule", "output_template_id")


def scope_column_present(s) -> bool:
    """Is pipeline.rule_scope there? Not mapped on the Pipeline model on
    purpose — a mapped column is selected by EVERY Pipeline query, which would
    fail on a database without migration 21 — so it is read and written here."""
    return _column_present(s, "pipeline", "rule_scope")


def pipeline_scope(s, pipeline_id: Optional[int]) -> Optional[dict]:
    """A setup's stored rule choice, normalised. None when it has none, when
    there is no such setup, or when the column does not exist yet."""
    if not pipeline_id or not scope_column_present(s):
        return None
    raw = s.execute(text("SELECT rule_scope FROM pipeline WHERE id = :id"),
                    {"id": int(pipeline_id)}).scalar()
    return normalize_scope(raw)


def write_pipeline_scope(s, pipeline_id: int, raw: Any) -> Optional[dict]:
    """Store a setup's rule choice (normalised; an empty or unreadable one is
    stored as SQL NULL — no choice). Returns what was stored. The caller commits.
    Raises LookupError when the column does not exist, so a choice somebody
    made is refused out loud rather than dropped."""
    if not scope_column_present(s):
        raise LookupError("pipeline.rule_scope is not available on this database")
    scope = normalize_scope(raw)
    s.execute(text("UPDATE pipeline SET rule_scope = CAST(:v AS JSONB) WHERE id = :id"),
              {"v": json.dumps(scope) if scope is not None else None,
               "id": int(pipeline_id)})
    return scope


def tag_select(s, alias: str = "") -> str:
    """The SELECT expression for a rule's template tag, as `rule_template_id`.
    NULL — "untagged" — where the column does not exist yet."""
    col = f"{alias}.output_template_id" if alias else "output_template_id"
    expr = col if tag_column_present(s) else "CAST(NULL AS INTEGER)"
    return f"{expr} AS rule_template_id"


def families(s, template_ids: Iterable[Optional[int]]) -> dict[int, set[int]]:
    """template id → ids of every version of that template (same tenant, same
    name). An id with no row maps to itself."""
    ids = sorted({int(t) for t in template_ids if t is not None})
    if not ids:
        return {}
    from db import ExportTemplate
    named = {t.id: (t.tenant_id, t.name) for t in
             s.query(ExportTemplate).filter(ExportTemplate.id.in_(ids)).all()}
    out: dict[int, set[int]] = {i: {i} for i in ids}
    keys = {k for k in named.values() if k[1]}
    if not keys:
        return out
    siblings: dict[tuple, set[int]] = {}
    for tid, tenant_id, name in (
            s.query(ExportTemplate.id, ExportTemplate.tenant_id, ExportTemplate.name)
            .filter(ExportTemplate.name.in_({k[1] for k in keys})).all()):
        if (tenant_id, name) in keys:
            siblings.setdefault((tenant_id, name), set()).add(tid)
    for i, key in named.items():
        out[i] = siblings.get(key, {i}) | {i}
    return out


def contract_templates(s, contract_ids: Iterable[Optional[int]]) -> dict[int, Optional[int]]:
    """contract id → the output template it is bound to (contract.output_template_id)."""
    ids = sorted({int(c) for c in contract_ids if c is not None})
    if not ids:
        return {}
    from db import Contract
    return {cid: tid for cid, tid in
            s.query(Contract.id, Contract.output_template_id)
            .filter(Contract.id.in_(ids)).all()}


def filter_rules(s, rules: list[dict], *, setup_template_id: Optional[int],
                 scope: Any = None) -> list[dict]:
    """The rules a setup runs, out of `rules` (each a dict carrying `rule_id`,
    `contract_id` and `rule_template_id` — select it with `tag_select`).

    With no scope and no tagged rule among them this returns `rules` untouched,
    so every setup and rule that existed before this module behaves exactly as
    it did, without a single extra query."""
    scope = normalize_scope(scope)
    if scope is None and all(r.get("rule_template_id") is None for r in rules):
        return rules
    c_tpl = contract_templates(s, (r.get("contract_id") for r in rules))
    wanted = [setup_template_id]
    if scope and scope.get("template_ids"):
        wanted += scope["template_ids"]
    wanted += [effective_template(r.get("rule_template_id"), c_tpl.get(r.get("contract_id")))
               for r in rules]
    fam = families(s, wanted)
    setup_family = fam.get(setup_template_id, set()) if setup_template_id else set()
    return [r for r in rules if rule_applies(
        rule_id=r.get("rule_id"), rule_tag=r.get("rule_template_id"),
        contract_template=c_tpl.get(r.get("contract_id")),
        setup_family=setup_family, scope=scope, family_of=fam)]


def rule_sets(s, contract_id: int) -> dict[Optional[int], int]:
    """A contract's live rule sets: effective template → number of rules.
    A key of None collects rules written for no template at all."""
    from db import Contract
    c = s.get(Contract, contract_id)
    bound = c.output_template_id if c else None
    rows = s.execute(text(
        f"SELECT {tag_select(s)}, COUNT(*) AS n FROM validation_rule "
        "WHERE contract_id = :cid AND rule_status <> 'disabled' "
        "GROUP BY 1"), {"cid": contract_id}).mappings().all()
    out: dict[Optional[int], int] = {}
    for r in rows:
        key = effective_template(r["rule_template_id"], bound)
        out[key] = out.get(key, 0) + int(r["n"])
    return out


def set_for_template(s, contract_id: int, template_id: int) -> tuple[Optional[int], int]:
    """(a template id the contract's rule set for this template is written
    against, how many live rules that set has) — matched by family, so a later
    version of the same template finds the set, and rules split across versions
    of one template count as the one set they are. (None, 0) when there is none."""
    sets = rule_sets(s, contract_id)
    if not sets:
        return None, 0
    fam = families(s, [template_id, *[k for k in sets if k is not None]])
    mine = fam.get(template_id, {template_id})
    hit, total = None, 0
    for tid, n in sets.items():
        if tid is not None and tid in mine:
            hit = tid if hit is None else hit
            total += n
    return hit, total


def same_family(s, a: Optional[int], b: Optional[int]) -> bool:
    """Are two template ids versions of one template?"""
    if a is None or b is None:
        return False
    if a == b:
        return True
    return b in families(s, [a]).get(a, {a})


def added_set_templates(s, contract_id: int) -> list[int]:
    """Template ids of the rule sets ADDED to a contract for other templates —
    every rule set it has that is not its own. Disabled rules included: a
    removed rule still belongs to its set. Empty for every contract that has
    never had a set added, which is what every guard built on this relies on
    to leave those contracts exactly as they were."""
    if not tag_column_present(s):
        return []
    return [int(t) for (t,) in s.execute(text(
        "SELECT DISTINCT output_template_id FROM validation_rule "
        "WHERE contract_id = :cid AND output_template_id IS NOT NULL "
        "ORDER BY 1"), {"cid": contract_id}).all()]
