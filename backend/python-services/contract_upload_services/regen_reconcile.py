"""
regen_reconcile.py
──────────────────
Regeneration stability: stop re-deciding settled things.

The problem this solves (measured on contracts 935/937, identical PDF): the
LLM re-reads the document on every setup and re-decides everything, so each
regeneration silently drifts — rules lost, values narrowed, columns re-bound,
one referral trigger inverted. temp=0 does not make Gemini deterministic.

Three pieces, used together by the upload routes + db_persister:

  1. IDENTITY LADDER — is this upload a version of a contract we already know?
       L1  raw file bytes hash            → same file, byte-for-byte
       L2  content_fingerprint (existing) → same doc text + same template fields
       L2b document-text hash alone       → same doc, different/edited template
       L3  extracted business identity    → amended version of a known contract
     L1/L2/L2b run BEFORE any LLM call (route-side). L3 needs the extraction
     output, so it runs at persist time. All are TENANT-wide: the same contract
     set up twice lands in different programs (935→766 vs 763), so a
     program-scoped lookup would never fire for the real workflow.

  2. RULE FINGERPRINT — identify a rule by what it DOES, never by its
     LLM-worded name: check kind + bound column(s) + canonical parameters +
     source anchor. Library rules are identified by their (stable) library
     name; contract rules never are, because the model rewords names run to
     run ("Commission Rate" vs "Commission Percentage" — same check).

  3. RECONCILE-AND-CARRY-FORWARD — given the prior contract's persisted rules
     and this run's freshly generated set, produce the set to persist:
       identical check            → carry the OLD row untouched (incl. its
                                    human-set status: an old 'disabled' rule
                                    STAYS disabled and suppresses the newly
                                    regenerated copy — a human said no once)
       same check, params drift   → carry OLD as-is + persist the new version
                                    as an INERT proposal (rule_status=
                                    'needs_review'; the engine only executes
                                    'active', so nothing changes until a human
                                    approves)
       same check, column drift   → same: old stands, new is a proposal
       only in old                → carry it forward (never silently lose
                                    coverage) and flag it in the report
       only in new                → proposal, never auto-active
     Principle: the old decision is the default; a new run only PROPOSES.

Proposals carry their reason inside rule_spec['regen'] (a JSON island — no new
column, no migration). The whole feature is behind KAVACHIO_CARRY_FORWARD
(default on); set =0 and every upload behaves exactly as before this module.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re


def _enabled() -> bool:
    return os.getenv("KAVACHIO_CARRY_FORWARD", "1") != "0"


# ─────────────────────────────────────────────────────────────────────────────
# 1. IDENTITY LADDER
# ─────────────────────────────────────────────────────────────────────────────

def file_sha256(file_bytes) -> str | None:
    """L1: hash of the exact bytes uploaded. Strongest, cheapest match."""
    if not file_bytes:
        return None
    return hashlib.sha256(file_bytes).hexdigest()


def doc_text_sha256(document_text) -> str | None:
    """L2b: hash of the normalised extracted text ALONE (no template folded in,
    unlike contract_versioning.compute_content_fingerprint). Matches the same
    document re-set-up against an edited template, where L2 misses."""
    if not document_text:
        return None
    norm = " ".join((document_text or "").split()).lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def identity_payload(file_bytes=None, document_text=None) -> dict:
    """The hashes an upload should stash in contract.extracted['identity'] so
    FUTURE uploads can L1/L2b-match it. Stored in the JSONB the app already
    owns — deliberately not a new column."""
    out = {}
    fs = file_sha256(file_bytes)
    ds = doc_text_sha256(document_text)
    if fs:
        out["file_sha256"] = fs
    if ds:
        out["doc_sha256"] = ds
    return out


def find_prior_contract(conn, tenant_id, content_fp=None, file_sha=None,
                        doc_sha=None, exclude_contract_id=None):
    """L1/L2/L2b lookup (pre-LLM). Returns
    {contract_id, level, output_template_id} or None.

    `conn` is a SQLAlchemy connection/engine-connect on the canonical DB.
    Tenant-wide, most recent current non-failed match wins. L2 works
    retroactively (content_fingerprint has been stamped for a while); L1/L2b
    only match contracts uploaded after this module started stashing
    extracted['identity'].
    """
    if not _enabled() or not tenant_id:
        return None
    from sqlalchemy import text

    probes = []
    if file_sha:
        probes.append(("L1", "c.extracted->'identity'->>'file_sha256' = :v", file_sha))
    if content_fp:
        probes.append(("L2", "c.row_hash = :v", content_fp))
    if doc_sha:
        probes.append(("L2b", "c.extracted->'identity'->>'doc_sha256' = :v", doc_sha))

    for level, cond, val in probes:
        row = conn.execute(
            text(f"""
                SELECT c.contract_id, c.output_template_id
                FROM   contract c
                WHERE  c.tenant_id = :tid
                  AND  {cond}
                  AND  COALESCE(c.status_ops, '') <> 'failed'
                  AND  (:skip IS NULL OR c.contract_id <> :skip)
                ORDER BY c.contract_id DESC
                LIMIT 1
            """),
            {"tid": tenant_id, "v": val, "skip": exclude_contract_id},
        ).first()
        if row:
            return {"contract_id": row[0], "level": level,
                    "output_template_id": row[1]}
    return None


def _norm_scalar(v):
    return " ".join(str(v or "").split()).lower()


def find_prior_contract_l3(conn, tenant_id, program_metadata,
                           exclude_contract_id=None):
    """L3 (post-extraction): an AMENDED version of a known contract — the text
    changed, so no hash matches, but the business identity is the same.

    Conservative on purpose: ALL of program name, document type and inception
    date must match (three independently extracted fields agreeing by accident
    across different contracts is vanishingly unlikely; any one alone is not).
    NOTE: the stored `umr` column is synthesised per-run from a randomised
    hash (db_persister) and is deliberately NOT used — it never matches.
    """
    if not _enabled() or not tenant_id or not isinstance(program_metadata, dict):
        return None

    def _meta(name):
        f = program_metadata.get(name)
        return _norm_scalar(f.get("value") if isinstance(f, dict) else f)

    pname, inception = _meta("program_name"), _meta("inception_date")
    if not pname or not inception:
        return None

    from sqlalchemy import text
    rows = conn.execute(
        text("""
            SELECT c.contract_id, c.output_template_id,
                   c.extracted->'program_metadata' AS meta,
                   c.contract_inception_date::text AS inception
            FROM   contract c
            WHERE  c.tenant_id = :tid
              AND  c.is_current_version IS TRUE
              AND  COALESCE(c.status_ops, '') <> 'failed'
              AND  (:skip IS NULL OR c.contract_id <> :skip)
            ORDER BY c.contract_id DESC
            LIMIT 200
        """),
        {"tid": tenant_id, "skip": exclude_contract_id},
    ).fetchall()

    for cid, tpl_id, meta, prior_inception in rows:
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = None
        if not isinstance(meta, dict):
            continue

        def _prior(name):
            f = meta.get(name)
            return _norm_scalar(f.get("value") if isinstance(f, dict) else f)

        if _prior("program_name") != pname:
            continue
        # inception: extracted value if present, else the stored column
        prior_inc = _prior("inception_date") or _norm_scalar(prior_inception)
        if prior_inc != inception:
            continue
        return {"contract_id": cid, "level": "L3",
                "output_template_id": tpl_id}
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 2. RULE FINGERPRINT
# ─────────────────────────────────────────────────────────────────────────────

# Params keys that name the column(s) a check runs against.
_FIELD_KEYS = ("field", "required_field", "result_field", "left_field",
               "right_field", "start_field", "end_field", "group_by",
               "zip_field", "state_field", "amount_field", "base_field")


def _spec_of(rule):
    spec = rule.get("rule_spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception:
            spec = {}
    return spec if isinstance(spec, dict) else {}


def _ir_of(rule):
    return _spec_of(rule).get("ir") or rule.get("ir") or {}


def _params_of(rule):
    return _ir_of(rule).get("params") or {}


def rule_kind(rule) -> str:
    return _ir_of(rule).get("template") or rule.get("template") or ""


def target_fields(rule) -> tuple:
    """Sorted tuple of every column name the rule binds. What the rule DOES is
    (kind, fields, params) — the LLM-worded name never participates."""
    p = _params_of(rule)
    out = set()
    for k in _FIELD_KEYS:
        v = p.get(k)
        if isinstance(v, str) and v:
            out.add(v)
    cond = p.get("condition")
    if isinstance(cond, dict) and cond.get("field"):
        out.add(cond["field"])
    for f in (p.get("fields") or []):
        if isinstance(f, str):
            out.add(f)
    return tuple(sorted(out))


def _canon_value(v):
    if isinstance(v, str):
        return _norm_scalar(v)
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    if isinstance(v, list):
        return sorted(json.dumps(_canon_value(x), default=str) for x in v)
    if isinstance(v, dict):
        return {k: _canon_value(x) for k, x in sorted(v.items())}
    return v


def core_params(rule) -> str:
    """Canonical JSON of the params that define behaviour. variation_values is
    excluded — alternate spellings drift between runs without changing what the
    check enforces (they are compared separately by the value-list guard)."""
    p = {k: v for k, v in _params_of(rule).items() if k != "variation_values"}
    return json.dumps(_canon_value(p), sort_keys=True, default=str)


def is_library_rule(rule) -> bool:
    if (rule.get("rule_source") or _ir_of(rule).get("rule_source")) == "generic_library":
        return True
    # Persisted rows: the library marker lives in the verbatim text.
    return str(rule.get("source_verbatim_text") or "").strip().startswith("[Generic rule]")


def _clause_anchor(rule) -> str:
    txt = rule.get("source_verbatim_text") or ""
    return re.sub(r"[^a-z0-9]", "", txt.lower())[:400]


def _name_norm(rule) -> str:
    return re.sub(r"[^a-z0-9]", "", str(rule.get("rule_name") or "").lower())


def values_narrowed(old_rule, new_rule):
    """Item 7 — value-list guard. True when the new rule's allowed/excluded set
    is a strict SUBSET of the old one's (same check, silently narrower). The
    935→937 run dropped an underwriter and a broker alias exactly this way."""
    po, pn = _params_of(old_rule), _params_of(new_rule)
    for key in ("allowed", "excluded"):
        old_v = {_norm_scalar(x) for x in (po.get(key) or []) if x is not None}
        new_v = {_norm_scalar(x) for x in (pn.get(key) or []) if x is not None}
        if old_v and new_v and new_v < old_v:
            return sorted(old_v - new_v)
    return None



# Writers that mean "a machine produced this", i.e. NOT a human decision. Any
# other value in created_by/updated_by is a person or a human-triggered action.
_MACHINE_WRITERS = {
    "ai_generator_v1", "ai_generator_ir_v1", "carry_forward_v1",
    "regen_proposal_v1", "system", "", None,
}


def is_human_decided(rule) -> bool:
    """Has a PERSON ever ruled on this rule?

    This is the distinction the first version of carry-forward missed, and it is
    the whole reason a known-bad rule survived three regenerations: it treated
    every prior rule as a settled decision, when in fact an untouched prior rule
    is not a decision at all — it is just the previous run's guess, carrying no
    more authority than this run's guess.

    A human decision is protected absolutely: never auto-replaced, never
    auto-paused. An unreviewed prior rule is protected only by inertia — it
    still wins ties (stability), but it loses to a demonstrably better answer.
    """
    # A carried row is WRITTEN by a machine (created_by='carry_forward_v1'),
    # so the flag has to ride along explicitly or the human's ruling is lost the
    # first time the rule is carried — and the final gate pass would then be free
    # to pause a rule a person had already approved.
    regen = (rule.get("rule_spec") or {}).get("regen") if isinstance(
        rule.get("rule_spec"), dict) else None
    if isinstance(regen, dict) and regen.get("human_decided"):
        return True
    if rule.get("reviewed_at") or rule.get("reviewed_by_user_id"):
        return True
    if (rule.get("approval_status") or "").lower() in ("approved", "rejected"):
        return True
    for who in (rule.get("updated_by"), rule.get("created_by")):
        if who and who not in _MACHINE_WRITERS:
            return True
    # A human is the only actor that disables a rule.
    return rule.get("rule_status") == "disabled"


def _gate_fails(rule, field_names):
    """Reasons this rule fails the deterministic provenance gates ([] = clean)."""
    if not field_names:
        return []
    try:
        from contract_upload_services.rule_normalizer import gate_failures
        return gate_failures(rule, field_names=field_names)
    except Exception:      # noqa: BLE001 — a gate problem must never block a save
        return []


def _value_set(rule):
    """Canonical allowed/excluded set — the identity of an enum check's content."""
    p = _params_of(rule)
    for k in ("allowed", "excluded"):
        if p.get(k):
            return (k, tuple(sorted(_norm_scalar(x) for x in p[k] if x is not None)))
    return None


def dedupe_behavioural_twins(rules, report=None):
    """Pause rules that enforce an IDENTICAL check under a different name.

    Two rules with the same check kind, the same bound column(s) and the same
    canonical parameters raise the SAME exception on the SAME row — the reviewer
    sees one violation reported twice and cannot tell which rule to act on. The
    LLM names rules freely, so nothing upstream can catch this by name; behaviour
    is the only reliable identity (the same reason rule fingerprints ignore names).

    Deterministic and order-stable: among twins, the survivor is the one a human
    has ruled on, else the one with the longest source text (the better-evidenced
    provenance), else the first. Only ACTIVE rules are considered — a proposal or
    a paused rule is not enforcing anything and cannot double-report.
    """
    groups = {}
    for r in rules:
        if not isinstance(r, dict) or r.get("rule_status") != "active":
            continue
        groups.setdefault(
            (rule_kind(r), target_fields(r), core_params(r)), []).append(r)

    paused = 0
    for _key, twins in groups.items():
        if len(twins) < 2:
            continue
        twins.sort(key=lambda r: (
            0 if is_human_decided(r) else 1,
            -len(str(r.get("source_verbatim_text") or "")),
        ))
        keeper, rest = twins[0], twins[1:]
        for dup in rest:
            dup["rule_status"] = "needs_review"
            spec = dict(dup.get("rule_spec") or {})
            spec["regen"] = {**(spec.get("regen") or {}),
                             "proposal": "duplicate_of_active_rule",
                             "duplicate_of": keeper.get("rule_name")}
            dup["rule_spec"] = spec
            desc = (dup.get("rule_description") or "").strip()
            dup["rule_description"] = (
                f"[Paused for review] Enforces the same check as "
                f"{keeper.get('rule_name')!r} on the same column(s) with the same "
                f"values — it would report every violation twice."
                + (f" Original rule: {desc}" if desc else ""))
            paused += 1
            if report is not None:
                report["details"].append({
                    "rule": dup.get("rule_name"), "change": "duplicate_paused",
                    "duplicate_of": keeper.get("rule_name")})
    if report is not None and paused:
        report["duplicates_paused"] = paused
    return rules


# ─────────────────────────────────────────────────────────────────────────────
# 3. RECONCILE-AND-CARRY-FORWARD
# ─────────────────────────────────────────────────────────────────────────────

def load_prior_rules(conn, contract_id):
    """The prior contract's persisted rules, shaped like the pipeline's rule
    dicts so reconcile() can compare and the persister can re-insert them."""
    from sqlalchemy import text
    rows = conn.execute(
        text("""
            SELECT vr.rule_id, vr.rule_engine, rcl.name AS rule_class,
                   vr.rule_name, vr.rule_description, vr.validation_stage,
                   vr.severity, vr.canonical_target, vr.rule_spec,
                   vr.error_message, vr.source_verbatim_text,
                   vr.source_page_number, vr.generation_confidence,
                   vr.rule_status, vr.approval_status, vr.reviewed_by_user_id,
                   vr.reviewed_at, vr.created_by, vr.updated_by
            FROM   validation_rule vr
            LEFT JOIN rule_class_library rcl
                   ON rcl.rule_class_id = vr.rule_class_id
            WHERE  vr.contract_id = :cid
              AND  vr.rule_status <> 'superseded'
        """),
        {"cid": contract_id},
    ).mappings().fetchall()

    out = []
    for r in rows:
        d = dict(r)
        for js in ("canonical_target", "rule_spec"):
            if isinstance(d.get(js), str):
                try:
                    d[js] = json.loads(d[js])
                except Exception:
                    pass
        d["prior_rule_id"] = d.pop("rule_id")
        out.append(d)
    return out


def _carried_row(old, new_match=None):
    """A persistable rule dict that reproduces the OLD row on the new contract.
    Anchors to the NEW run's clause when the check matched one (so the clause
    screens link correctly); keeps the old verbatim text either way."""
    spec = dict(_spec_of(old))
    regen = dict(spec.get("regen") or {})
    regen.update({"carried_from": old.get("prior_rule_id"),
                  "outcome": "carried"})
    # Preserve the human's authority across the carry (see is_human_decided).
    if is_human_decided(old):
        regen["human_decided"] = True
    spec["regen"] = regen
    return {
        "rule_engine":           old.get("rule_engine") or "ajv",
        "rule_class":            old.get("rule_class"),
        "rule_name":             old.get("rule_name"),
        "rule_description":      old.get("rule_description"),
        "validation_stage":      old.get("validation_stage"),
        "severity":              old.get("severity"),
        "canonical_target":      old.get("canonical_target") or {},
        "rule_spec":             spec,
        "error_message":         old.get("error_message"),
        "source_clause_id":      (new_match or {}).get("source_clause_id"),
        "source_verbatim_text":  old.get("source_verbatim_text"),
        "source_page_number":    old.get("source_page_number"),
        "generation_confidence": old.get("generation_confidence"),
        # THE point of carry-forward: a human-set status is a settled decision.
        # 'disabled' stays disabled; 'needs_review' stays queued; 'active' stays
        # active. The new run cannot promote or revive anything by itself.
        "rule_status":           old.get("rule_status") or "active",
        "created_by":            "carry_forward_v1",
    }


def _proposal(new_rule, reason, old=None, extra=None):
    """The new run's version, persisted INERT (needs_review). The engine only
    executes rule_status='active' (validation_routes), so a proposal changes
    nothing until a human approves it — that is the sign-off gate."""
    r = dict(new_rule)
    spec = dict(_spec_of(r))
    spec["regen"] = {
        "proposal": reason,
        "supersedes": (old or {}).get("prior_rule_id"),
        **(extra or {}),
    }
    r["rule_spec"] = spec
    r["rule_status"] = "needs_review"
    r["created_by"] = "regen_proposal_v1"
    # A proposal sits beside the rule it would replace and carries the SAME
    # name, so without this a flat listing shows two rows that look identical.
    # Description is where this codebase already puts that signal (_cc_downgrade
    # does the same for paused rules) because every rule screen renders it.
    _WHY = {
        "column_changed": "binds a different column",
        "params_changed": "uses different values or thresholds",
        "values_narrowed": "drops values the enforced rule still checks",
        "new_rule": "was not present on the previous version",
    }
    desc = (r.get("rule_description") or "").strip()
    r["rule_description"] = (
        f"[Proposed change — not enforced] This run {_WHY.get(reason, 'differs')}"
        f" compared with the rule currently enforced. Approve to switch to it, "
        f"or reject to keep the active rule."
        + (f" Original rule: {desc}" if desc else ""))
    # A proposal keeps the SAME name as the rule it would replace, so a plain
    # listing of the contract's rules shows two rows that look identical and
    # read as an accidental duplicate. Suffix the name so every view — the rule
    # list, an export, a raw SQL query — distinguishes the enforced rule from
    # the suggestion without having to inspect rule_status.
    r["rule_name"] = _suffixed_name(r.get("rule_name"), "proposed alternative")
    return r


_REGEN_NAME_SUFFIXES = ("proposed alternative", "restore from earlier version")


def _suffixed_name(name, suffix):
    """`name — suffix`, without stacking a suffix that is already present."""
    base = (name or "Unnamed rule").strip()
    for s in _REGEN_NAME_SUFFIXES:
        marker = f" \u2014 {s}"
        if base.endswith(marker):
            base = base[: -len(marker)]
    return f"{base} \u2014 {suffix}"


def reconcile(old_rules, new_rules, same_template=True, field_names=None):
    """Merge the prior contract's rules with this run's generated set.

    Returns (rules_to_persist, report). With same_template=False (an L2b/L3
    match against a different template) old compiled SQL cannot be trusted to
    run against the new sheets, so nothing is carried — the new set stands, and
    the report still calls out params drift and narrowed value lists so the
    reviewer sees what changed.
    """
    report = {"carried": 0, "identical": 0, "params_changed": 0,
              "column_changed": 0, "only_in_old": 0, "only_in_new": 0,
              "suppressed_by_disabled": 0, "values_narrowed": 0,
              "replaced_failing_gate": 0, "carried_paused_by_gate": 0,
              "duplicates_paused": 0,
              "same_template": bool(same_template), "details": []}

    if not old_rules:
        return dedupe_behavioural_twins(list(new_rules), report), report

    unmatched_new = list(new_rules)
    matched_pairs = []          # (old, new, how)

    def _take(new_rule):
        unmatched_new.remove(new_rule)

    # ── Tier 0: library rules by their stable library name ──────────────────
    new_lib = {}
    for r in unmatched_new:
        if is_library_rule(r):
            new_lib.setdefault(_name_norm(r), r)
    for old in old_rules:
        if not is_library_rule(old):
            continue
        cand = new_lib.pop(_name_norm(old), None)
        if cand is not None and cand in unmatched_new:
            matched_pairs.append((old, cand, "library_name"))
            _take(cand)

    matched_old = {id(o) for o, _, _ in matched_pairs}

    # ── Tier 1/2: contract rules by (kind, fields) — exact behaviour first ──
    for old in old_rules:
        if id(old) in matched_old or is_library_rule(old):
            continue
        key = (rule_kind(old), target_fields(old))
        best = None
        for cand in unmatched_new:
            if is_library_rule(cand):
                continue
            if (rule_kind(cand), target_fields(cand)) != key:
                continue
            if core_params(cand) == core_params(old):
                best = cand
                break                      # exact behaviour — take it
            if best is None:
                best = cand                # params drift on the same check
        if best is not None:
            matched_pairs.append((old, best, "kind_fields"))
            matched_old.add(id(old))
            _take(best)

    # ── Tier 3: same check re-bound to a DIFFERENT column. Match on kind +
    # clause-anchor / name similarity (the Excel-audit pairing, proven on the
    # real 935/937 drift: 'Layer Limit' Pol Occ Limit → Reinsurance Limit). ──
    for old in old_rules:
        if id(old) in matched_old:
            continue
        best, best_score = None, 0.0
        for cand in unmatched_new:
            if is_library_rule(cand) != is_library_rule(old):
                continue
            if rule_kind(cand) != rule_kind(old):
                continue
            score = 0.0
            ao, an = _clause_anchor(old), _clause_anchor(cand)
            if ao and an:
                score += 2 * difflib.SequenceMatcher(None, ao[:200], an[:200]).ratio()
            score += difflib.SequenceMatcher(None, _name_norm(old), _name_norm(cand)).ratio()
            if set(target_fields(old)) & set(target_fields(cand)):
                score += 0.5
            if score > best_score:
                best, best_score = cand, score
        if best is not None and best_score >= 1.6:
            matched_pairs.append((old, best, "anchor"))
            matched_old.add(id(old))
            _take(best)

    # ── Emit ────────────────────────────────────────────────────────────────
    out = []
    for old, new, how in matched_pairs:
        identical = (core_params(old) == core_params(new)
                     and target_fields(old) == target_fields(new))
        if old.get("rule_status") == "disabled":
            # A human turned this check off. The regenerated copy is dropped,
            # the disabled row rides along so the decision itself is visible.
            report["suppressed_by_disabled"] += 1
            if same_template:
                out.append(_carried_row(old, new))
            continue
        if not same_template:
            # Different template: the new binding stands; only report drift.
            out.append(new)
            if not identical:
                report["params_changed"] += 1
                report["details"].append(
                    {"rule": old.get("rule_name"), "change": "params_changed",
                     "note": "different template — new binding kept"})
            continue
        if identical:
            report["identical"] += 1
            out.append(_carried_row(old, new))
            continue

        # ── Which answer is better EVIDENCED? ───────────────────────────────
        # The old rule wins ties, but "the previous guess" is not evidence. When
        # the old binding fails a deterministic provenance gate and the new one
        # does not, the new answer is demonstrably better and carrying the old
        # would re-admit a known defect on every future upload — which is exactly
        # how a mapping that contradicted its own reasoning survived three
        # regenerations. A HUMAN decision is never overridden this way.
        old_fails = _gate_fails(old, field_names)
        if old_fails and not is_human_decided(old):
            if not _gate_fails(new, field_names):
                report["replaced_failing_gate"] += 1
                report["details"].append({
                    "rule": old.get("rule_name"),
                    "change": "replaced_old_failed_gate",
                    "why": old_fails[0][:300]})
                promoted = dict(new)
                spec = dict(_spec_of(promoted))
                spec["regen"] = {"outcome": "replaced_old_failing_gate",
                                 "supersedes": old.get("prior_rule_id"),
                                 "old_defect": old_fails[0][:300]}
                promoted["rule_spec"] = spec
                promoted["created_by"] = "regen_replaced_v1"
                out.append(promoted)
                continue
            # BOTH sides carry the defect: carrying it active would ship a known
            # bad rule, so carry it PAUSED with the reason a human needs.
            report["carried_paused_by_gate"] += 1
            report["details"].append({
                "rule": old.get("rule_name"), "change": "carried_paused_by_gate",
                "why": old_fails[0][:300]})
            row = _carried_row(old, new)
            row["rule_status"] = "needs_review"
            row["rule_spec"]["regen"]["outcome"] = "carried_paused_by_gate"
            out.append(row)
            continue

        # Same check, something drifted → old stands, new becomes a proposal.
        narrowed = values_narrowed(old, new)
        if narrowed:
            report["values_narrowed"] += 1
            change, extra = "values_narrowed", {"dropped_values": narrowed}
        elif target_fields(old) != target_fields(new):
            report["column_changed"] += 1
            change, extra = "column_changed", {
                "old_fields": list(target_fields(old)),
                "new_fields": list(target_fields(new))}
        else:
            report["params_changed"] += 1
            change, extra = "params_changed", {}
        report["details"].append({"rule": old.get("rule_name"),
                                  "change": change, **extra})
        out.append(_carried_row(old, new))
        out.append(_proposal(new, change, old, extra))

    _emitted = {(rule_kind(r), target_fields(r)) for r in out if rule_kind(r)}
    for old in old_rules:
        if id(old) in matched_old or old.get("rule_status") == "superseded":
            continue
        # Already covered by something emitted above — typically a rule the
        # fresh run REBOUND and that replaced its failing twin. Carrying it too
        # would put a second row for the same check on the same column into the
        # set, which reads as a duplicate to anyone listing the contract's rules.
        if (rule_kind(old), target_fields(old)) in _emitted:
            continue
        # Not regenerated this run. NEVER silently lose coverage: carry it
        # (with its old status) and flag it so the report shows the gap.
        report["only_in_old"] += 1
        report["details"].append({"rule": old.get("rule_name"),
                                  "change": "not_regenerated"})
        if same_template:
            row = _carried_row(old)
            row["rule_spec"]["regen"]["outcome"] = "carried_not_regenerated"
            out.append(row)

    for new in unmatched_new:
        report["only_in_new"] += 1
        report["details"].append({"rule": new.get("rule_name"),
                                  "change": "new_rule"})
        out.append(_proposal(new, "new_rule") if same_template else new)

    # ── Final policing of the SET, not just the pairs ───────────────────────
    # Carried rules never passed through the normalizer, and only-in-old rules
    # were never compared to anything, so this is the one place every rule about
    # to be persisted can be checked under the same deterministic gates. Human
    # decisions are exempt (see is_human_decided).
    if field_names:
        try:
            from contract_upload_services.rule_normalizer import (
                apply_provenance_gates,
            )
            policed = [r for r in out
                       if r.get("rule_status") == "active" and not is_human_decided(r)]
            before = {id(r): r.get("rule_status") for r in policed}
            apply_provenance_gates(policed, field_names=field_names)
            for r in policed:
                if r.get("rule_status") != before[id(r)]:
                    report["carried_paused_by_gate"] += 1
                    report["details"].append({
                        "rule": r.get("rule_name"),
                        "change": "paused_by_gate_on_final_set"})
        except Exception:  # noqa: BLE001 — never block a save on a gate
            pass

    # Twins raise the same exception twice on the same row; pause the copies.
    dedupe_behavioural_twins(out, report)

    report["carried"] = sum(1 for r in out
                            if r.get("created_by") == "carry_forward_v1")
    return out, report


def find_contract_lineage(conn, tenant_id, content_fp=None, file_sha=None,
                          doc_sha=None, exclude_contract_id=None, limit=6):
    """EVERY known prior version of this contract, newest first.

    find_prior_contract returns only the newest match, which is right for
    deciding "have I seen this before" but wrong for deciding what coverage this
    contract should have. A rule can be bound in version 1, missed by the model
    in version 2, and then be invisible to version 3 — the loss compounds
    silently, one generation at a time, and no single pairwise comparison can
    see it. Reading the whole lineage lets a check that was demonstrably
    bindable on this exact template be RECOVERED (as a proposal, never
    auto-active) instead of disappearing for good.
    """
    if not _enabled() or not tenant_id:
        return []
    from sqlalchemy import text
    conds, params = [], {"tid": tenant_id, "skip": exclude_contract_id}
    if content_fp:
        conds.append("c.row_hash = :fp"); params["fp"] = content_fp
    if file_sha:
        conds.append("c.extracted->'identity'->>'file_sha256' = :fs"); params["fs"] = file_sha
    if doc_sha:
        conds.append("c.extracted->'identity'->>'doc_sha256' = :ds"); params["ds"] = doc_sha
    if not conds:
        return []
    rows = conn.execute(
        text(f"""
            SELECT c.contract_id, c.output_template_id
            FROM   contract c
            WHERE  c.tenant_id = :tid
              AND  ({' OR '.join(conds)})
              AND  COALESCE(c.status_ops, '') <> 'failed'
              AND  (:skip IS NULL OR c.contract_id <> :skip)
            ORDER BY c.contract_id DESC
            LIMIT {int(limit)}
        """), params).fetchall()
    return [{"contract_id": r[0], "output_template_id": r[1]} for r in rows]


def recover_lost_coverage(conn, lineage_ids, kept_rules, report=None):
    """Re-surface checks an OLDER version bound but the current set has lost.

    Strictly additive and strictly inert: a recovered rule is persisted as
    `needs_review`, so it changes no validation outcome until a human approves
    it. That asymmetry is deliberate — recovering a rule that was deliberately
    removed only costs a review click, while losing a critical check silently
    costs a missed exception on every bordereau.

    Identity is behavioural (kind + bound columns), never the LLM-worded name,
    so a check that came back under a new name is not recovered twice.
    """
    if not lineage_ids:
        return []
    # A check counts as COVERED when the current set binds the same kind to the
    # same column(s), OR when it reproduces the same kind from the same source
    # clause on a different column. The second test matters: a re-binding is
    # already reported as `column_changed` by reconcile, so recovering it here
    # too would surface the same decision twice. Only a check nothing in the
    # current set reproduces at all is genuinely lost.
    # A check counts as COVERED only when NO VALUE THE OLD RULE LISTED HAS
    # DISAPPEARED. Matching the check kind and column is not enough: an enum rule
    # on the same column can carry a different or smaller value set and quietly
    # stop covering what it used to (a referral for two states replaced by one
    # naming six unrelated territories; an authorised-name list that dropped a
    # name; a carve-out that lost three spellings of the same carrier). Every
    # value the old rule enumerated must still appear in some current rule on the
    # same column and polarity — a vanished value is a real loss and is
    # re-surfaced for review, never silently accepted.
    by_key = {}
    for r in kept_rules:
        by_key.setdefault((rule_kind(r), target_fields(r)), []).append(r)
    have_anchor = {(rule_kind(r), _clause_anchor(r))
                   for r in kept_rules if _clause_anchor(r)}

    def _covered(old):
        cands = by_key.get((rule_kind(old), target_fields(old)))
        if not cands:
            return False
        ov = _value_set(old)
        if ov is None:
            return True            # nothing enumerated to lose
        for cand in cands:
            cv = _value_set(cand)
            if cv is None:
                continue
            if cv[0] == ov[0] and set(ov[1]) <= set(cv[1]):
                return True        # same polarity, and nothing dropped
        return False

    # ONE recovery per check. Several lineage versions usually hold the same
    # check with slightly different value sets (a carve-out with four spellings
    # in one version and one spelling in the next), and emitting each would put
    # near-identical rows in the review queue for a reviewer to tell apart. The
    # check is keyed by BEHAVIOUR — kind + bound columns — and the version kept
    # is the one enumerating the MOST values, since that is the one whose loss
    # actually costs coverage.
    chosen = {}
    for cid in lineage_ids:
        for old in load_prior_rules(conn, cid):
            if old.get("rule_status") not in ("active",):
                continue
            key = (rule_kind(old), target_fields(old))
            if not rule_kind(old) or _covered(old):
                continue
            anchor = _clause_anchor(old)
            if anchor and (rule_kind(old), anchor) in have_anchor and _value_set(old) is None:
                continue          # same clause, re-bound elsewhere — not lost
            prev = chosen.get(key)
            if prev is not None:
                pv, ov = _value_set(prev[0]), _value_set(old)
                if not (ov and (not pv or len(ov[1]) > len(pv[1]))):
                    continue      # already have an equal or more complete copy
            chosen[key] = (old, cid)

    recovered = []
    for (old, cid) in chosen.values():
        row = _carried_row(old)
        row["rule_status"] = "needs_review"
        row["created_by"] = "regen_recovered_v1"
        row["rule_spec"]["regen"] = {
            "outcome": "recovered_from_lineage",
            "recovered_from_contract": cid,
            "carried_from": old.get("prior_rule_id"),
        }
        desc = (row.get("rule_description") or "").strip()
        row["rule_description"] = (
            f"[Paused for review] This check was generated and bound on an "
            f"earlier version of this contract but no rule in the current run "
            f"covers it. Approve to restore it."
            + (f" Original rule: {desc}" if desc else ""))
        row["rule_name"] = _suffixed_name(row.get("rule_name"),
                                          "restore from earlier version")
        recovered.append(row)
        if report is not None:
            report["details"].append({
                "rule": old.get("rule_name"),
                "change": "recovered_from_lineage",
                "from_contract": cid})
    if report is not None:
        report["recovered_from_lineage"] = len(recovered)
    return recovered
