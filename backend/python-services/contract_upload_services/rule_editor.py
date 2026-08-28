"""Edit an existing validation rule after upload.

Currently: retarget an IR rule's OUTPUT FIELD (deterministic — NO LLM). The
output field is woven into several coupled artifacts (rule_spec.ir params,
rule_spec.compiled_sql, canonical_target.output_field/output_fields); changing
only some of them leaves a silent wrong-column rule, because the runtime
(`duckdb_validation._compiled_sql_for`) executes rule_spec.compiled_sql verbatim
with no hash check. So we re-derive ALL of them from the IR, exactly mirroring
the upload-time build path (rule_normalizer.verify_and_build_ir_rule):

    remap_ir_fields(ir, old->new)  ->  validate_ir  ->  compile_ir

`compile_ir` bakes the field name in as a quoted identifier AND resolves the
FROM sheet(s) via the output schema, so a string-rename is never safe — only a
recompile is.
"""
from __future__ import annotations

import json
from typing import Any

from contract_upload_services.rule_ir import field_refs, remap_ir_fields, validate_ir
from contract_upload_services.rule_compiler import compile_ir, CompileError


class RetargetError(Exception):
    """Raised when a rule cannot be retargeted to the requested output field.
    The message is safe to surface to the user (HTTP 400)."""


class RuleEditError(Exception):
    """Raised when a rule's parameters cannot be edited as requested.
    The message is safe to surface to the user (HTTP 400)."""


# The only params a user may tune on a cross_field_math rule. Both are percents
# (1.0 == 1%): `tolerance_pct` is the compliant band; `reject_pct`, when set above
# it, turns the band beyond it into a hard violation and the middle into a warning
# (see rule_compiler._b_cross_field_math). Kept to a strict allowlist so this edit
# path can never rewrite field-valued params or the rule's structure.
_TOLERANCE_PARAMS = ("tolerance_pct", "reject_pct")

# Templates whose matching can be widened with an extra SURFACE SPELLING of a
# value the contract already names. Split because the two families store the
# spellings differently and mean opposite things:
#   enum        — `variation_values` is a FLAT LIST covering allowed/excluded, and
#                 a new spelling widens the set the rule compares against.
#   conditional — the spellings belong to the TARGET value only, and may be stored
#                 as a {value: [spellings]} MAP whose other keys are the
#                 CONDITION's values. Appending under the wrong key silently
#                 teaches the rule to accept the condition as an outcome.
_ENUM_VARIATION_TEMPLATES = ("value_in_set", "value_not_in_set")
_COND_VARIATION_TEMPLATES = ("conditional_value", "conditional_all")
VARIATION_TEMPLATES = _ENUM_VARIATION_TEMPLATES + _COND_VARIATION_TEMPLATES


def _as_dict(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


def retarget_ir_rule(rule_spec: Any, canonical_target: Any, error_message: Any,
                     old_field: str | None, new_field: str,
                     output_schema) -> tuple[dict, dict, Any, str, str]:
    """Return (new_rule_spec, new_canonical_target, new_error_message,
    resolved_old, resolved_new) with `old_field` repointed to `new_field` across
    the IR, the compiled SQL, canonical_target and the human error message.
    Deterministic; raises RetargetError on any invalid input.

    Only IR rules (rule_spec.kind == 'ir_v1') are supported — legacy ajv/custom
    rules store field names elsewhere and have no compiled_sql to regenerate.
    """
    spec = _as_dict(rule_spec)
    target = _as_dict(canonical_target)

    if spec.get("kind") != "ir_v1" or not isinstance(spec.get("ir"), dict):
        raise RetargetError(
            "Only AI rule-engine (IR) rules can be retargeted. Re-upload the "
            "contract to regenerate this rule for a different field.")

    # Resolve the user's chosen field to the EXACT template column name.
    resolved_new = output_schema.resolve_field(new_field)
    if not resolved_new:
        raise RetargetError(
            f"'{new_field}' is not a field in this contract's output template "
            "(or it is ambiguous across sheets).")

    ir = spec["ir"]
    current = field_refs(ir)
    if not current:
        raise RetargetError("This rule references no output fields; cannot retarget.")

    # Which existing field are we moving? Prefer the caller's old_field (resolved);
    # for a single-field rule, fall back to its only field.
    resolved_old = output_schema.resolve_field(old_field) if old_field else None
    target_old = resolved_old or old_field
    if target_old not in current:
        if len(current) == 1:
            target_old = current[0]
        else:
            raise RetargetError(
                f"This rule does not currently target '{old_field}'. It targets: "
                f"{', '.join(current)}.")

    if target_old == resolved_new:
        raise RetargetError("The rule already targets that output field.")

    # Don't collapse two of the rule's fields onto the same column — that would
    # silently produce a self-referential rule (e.g. Expiry < Expiry).
    if resolved_new in current and resolved_new != target_old:
        raise RetargetError(
            f"This rule also uses '{resolved_new}' for another field. Pick a "
            "field the rule doesn't already reference.")

    # Repoint ONLY target_old -> resolved_new; leave the rule's other fields intact.
    new_ir = remap_ir_fields(ir, lambda n: resolved_new if n == target_old else n)

    ok, reason = validate_ir(new_ir, set(output_schema.field_names))
    if not ok:
        raise RetargetError(f"Cannot retarget to '{resolved_new}': {reason}")

    try:
        new_sql = compile_ir(new_ir, output_schema.field_to_sheets,
                             getattr(output_schema, "primary_sheet", None),
                             aliases=getattr(output_schema, "field_aliases", None))
    except CompileError as exc:
        raise RetargetError(
            f"Cannot build a validation query for '{resolved_new}': {exc}")

    new_refs = field_refs(new_ir)

    new_spec = dict(spec)
    new_spec["ir"] = new_ir
    new_spec["compiled_sql"] = new_sql
    new_spec["executable"] = True
    new_spec.pop("unmapped_reason", None)
    # Mirror keys some legacy readers look at.
    if isinstance(new_spec.get("field"), str) and new_spec.get("field") == target_old:
        new_spec["field"] = resolved_new

    new_target = dict(target)
    new_target["output_field"] = new_refs[0] if new_refs else resolved_new
    new_target["output_fields"] = new_refs
    new_target["unmapped"] = False

    # Keep the human violation message from naming the old field (it also feeds
    # the substring-based "related clause" index in direct_routes).
    new_error_message = error_message
    if isinstance(error_message, str) and target_old and target_old in error_message:
        new_error_message = error_message.replace(target_old, resolved_new)

    return new_spec, new_target, new_error_message, target_old, resolved_new


def patch_tolerance(rule_spec: Any, output_schema, updates: dict) -> tuple[dict, dict]:
    """Return (new_rule_spec, applied) with the tolerance-band params on a
    cross_field_math IR rule updated and the compiled SQL regenerated
    deterministically (validate_ir → compile_ir → rebuild spec — the same path as
    retarget_ir_rule, so the runtime never runs a stale query).

    `updates` maps a key in `_TOLERANCE_PARAMS` to a percent (float ≥ 0), or to
    None to REMOVE that param (reverting to the single-threshold default).
    Deterministic; raises RuleEditError on any invalid input — safe for HTTP 400.
    """
    spec = _as_dict(rule_spec)
    if spec.get("kind") != "ir_v1" or not isinstance(spec.get("ir"), dict):
        raise RuleEditError(
            "Only AI rule-engine (IR) rules can be edited. Re-upload the contract "
            "to regenerate this rule.")

    ir = spec["ir"]
    if ir.get("template") != "cross_field_math":
        raise RuleEditError(
            "Tolerance bands apply only to cross-field math (formula) rules such "
            "as premium = base × rate.")

    clean: dict[str, float | None] = {}
    for key, val in (updates or {}).items():
        if key not in _TOLERANCE_PARAMS:
            raise RuleEditError(f"Unknown tolerance parameter: {key!r}")
        if val is None:
            clean[key] = None
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            raise RuleEditError(f"{key} must be a number (percent), got {val!r}")
        if num < 0:
            raise RuleEditError(f"{key} cannot be negative")
        clean[key] = num
    if not clean:
        raise RuleEditError("No tolerance parameters supplied.")

    new_params = dict(ir.get("params") or {})
    for key, val in clean.items():
        if val is None:
            new_params.pop(key, None)
        else:
            new_params[key] = val

    # A reject band that isn't strictly wider than the compliant band is inert
    # (the compiler would ignore it). Reject the edit rather than silently store a
    # no-op the UI would render as "bands on".
    tol = float(new_params.get("tolerance_pct") or 0)
    rej = new_params.get("reject_pct")
    if rej is not None and float(rej) <= tol:
        raise RuleEditError(
            "reject_pct must be greater than tolerance_pct to form a "
            "warn/reject band.")

    new_ir = dict(ir)
    new_ir["params"] = new_params

    ok, reason = validate_ir(new_ir, set(output_schema.field_names))
    if not ok:
        raise RuleEditError(f"Cannot apply tolerance: {reason}")

    try:
        new_sql = compile_ir(new_ir, output_schema.field_to_sheets,
                             getattr(output_schema, "primary_sheet", None),
                             aliases=getattr(output_schema, "field_aliases", None))
    except CompileError as exc:
        raise RuleEditError(f"Cannot rebuild the validation query: {exc}")

    new_spec = dict(spec)
    new_spec["ir"] = new_ir
    new_spec["compiled_sql"] = new_sql
    new_spec["executable"] = True
    new_spec.pop("unmapped_reason", None)
    return new_spec, clean


# =====================================================================
# Surface spellings (variation_values) on the value-matching templates
# =====================================================================

def _vnorm(s) -> str:
    """lower + strip every non-alphanumeric — the same key the compiler compares
    cells on (rule_compiler._norm_py), so 'PALMS SPECIALTY' and 'Palms Specialty'
    are one spelling, not two."""
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", str(s).lower())


def _flat(vv) -> list:
    """`variation_values` has three live shapes — a flat list, a {value: [spellings]}
    map, or absent. Flatten to a list for DISPLAY and comparison only; the append
    path preserves whichever shape the rule actually stores."""
    if isinstance(vv, dict):
        out = []
        for spellings in vv.values():
            out.extend(spellings if isinstance(spellings, list) else [spellings])
        return out
    return list(vv or [])


def variation_context(rule_spec: Any) -> dict | None:
    """What the UI and the admission checks need to reason about one rule's
    spellings, or None when the rule has no spellings to widen.

    Returns {template, field, allowed, excluded, value, base, existing}, where
    `base` is the set of values THE CONTRACT ITSELF names — the only things a new
    spelling is ever allowed to be another way of writing:

        value_in_set      -> allowed   (a spelling here widens what PASSES)
        value_not_in_set  -> excluded  (a spelling here widens what is CAUGHT)
        conditional_*     -> [value]   (the enforced outcome, never the condition)
    """
    spec = _as_dict(rule_spec)
    ir = spec.get("ir")
    if spec.get("kind") != "ir_v1" or not isinstance(ir, dict):
        return None
    template = ir.get("template")
    if template not in VARIATION_TEMPLATES:
        return None
    params = ir.get("params") or {}
    if not isinstance(params, dict):
        return None

    allowed = list(params.get("allowed") or [])
    excluded = list(params.get("excluded") or [])
    value = params.get("value")
    if template == "value_in_set":
        base = allowed
    elif template == "value_not_in_set":
        base = excluded
    else:
        base = [value] if value is not None else []

    return {
        "template": template,
        "field": params.get("field"),
        "allowed": allowed,
        "excluded": excluded,
        "value": value,
        "base": base,
        "existing": _flat(params.get("variation_values")),
    }


def add_variation_value(rule_spec: Any, output_schema, spelling: str) -> tuple[dict, list, dict]:
    """Return (new_rule_spec, spellings_after, context) with `spelling` added to a
    value-matching rule's accepted surface forms and the compiled SQL regenerated
    deterministically (validate_ir → compile_ir → rebuild spec — the same path as
    patch_tolerance, so the runtime never executes a query that disagrees with the
    IR beside it).

    APPEND ONLY. There is no removal counterpart on purpose: `variation_values`
    is seeded with the contract's OWN authorized values (normalize_variation_values
    puts `base` first), so a "remove" affordance on the same list would let someone
    delete a value the contract actually names.

    Deterministic — no LLM. Whether a spelling DESERVES to be added is decided
    before this is called (contract_upload_services/variation_admit.py); this
    function only applies an already-approved one. Raises RuleEditError on any
    invalid input — safe for HTTP 400.
    """
    spec = _as_dict(rule_spec)
    if spec.get("kind") != "ir_v1" or not isinstance(spec.get("ir"), dict):
        raise RuleEditError(
            "Only AI rule-engine (IR) rules can be edited. Re-upload the contract "
            "to regenerate this rule.")

    ctx = variation_context(spec)
    if ctx is None:
        raise RuleEditError(
            "Spellings apply only to rules that match a value against the "
            "contract — an allowed/prohibited list, or a conditional value.")
    if not ctx["field"]:
        raise RuleEditError("This rule is not bound to an output column yet.")

    clean = str(spelling or "").strip()
    if not clean:
        raise RuleEditError("Enter a spelling to add.")

    ir = spec["ir"]
    params = ir.get("params") or {}
    vv = params.get("variation_values")
    template = ctx["template"]

    if template in _COND_VARIATION_TEMPLATES and isinstance(vv, dict):
        # Keyed map: the spellings of the TARGET value live under the key that
        # matches params["value"]. Every other key belongs to the CONDITION side —
        # appending there would teach the rule that the condition is an acceptable
        # outcome. Match on the normalized key so "CRC Group" and "crc group" are
        # the same bucket; create the key when the target has no bucket yet.
        target_key = None
        want = _vnorm(ctx["value"])
        for k in vv:
            if _vnorm(k) == want:
                target_key = k
                break
        if target_key is None:
            target_key = str(ctx["value"])
        bucket = vv.get(target_key)
        bucket = list(bucket) if isinstance(bucket, list) else ([bucket] if bucket else [])
        seen = {_vnorm(x) for x in bucket}
        if _vnorm(clean) not in seen:
            bucket.append(clean)
        new_vv: Any = {**vv, target_key: bucket}
        after = _flat(new_vv)
    else:
        # Flat list. When the rule has no spellings yet, seed from the contract's
        # own values first — normalize_variation_values does the same at build
        # time, and the compiler treats the list as the WHOLE match set, so an
        # unseeded list of one user spelling would narrow the rule to that alone.
        existing = list(vv) if isinstance(vv, list) else []
        if not existing:
            existing = list(ctx["allowed"]) + list(ctx["excluded"])
            if not existing and ctx["value"] is not None:
                existing = [ctx["value"]]
        merged, seen = [], set()
        for v in [*existing, clean]:
            k = _vnorm(v)
            if not k or k in seen:
                continue
            seen.add(k)
            merged.append(v)
        new_vv = merged
        after = list(merged)

    new_params = {**params, "variation_values": new_vv}
    new_ir = dict(ir)
    new_ir["params"] = new_params

    ok, reason = validate_ir(new_ir, set(output_schema.field_names))
    if not ok:
        raise RuleEditError(f"Cannot add this spelling: {reason}")

    try:
        new_sql = compile_ir(new_ir, output_schema.field_to_sheets,
                             getattr(output_schema, "primary_sheet", None),
                             aliases=getattr(output_schema, "field_aliases", None))
    except CompileError as exc:
        raise RuleEditError(f"Cannot rebuild the validation query: {exc}")

    new_spec = dict(spec)
    new_spec["ir"] = new_ir
    new_spec["compiled_sql"] = new_sql
    new_spec["executable"] = True
    new_spec.pop("unmapped_reason", None)
    return new_spec, after, ctx


def remove_variation_value(rule_spec: Any, output_schema, spelling: str) -> tuple[dict, list, dict]:
    """Return (new_rule_spec, spellings_after, context) with `spelling` REMOVED
    from a value-matching rule's accepted surface forms, SQL recompiled.

    The counterpart to add_variation_value, and deliberately narrower than it:

      A VALUE THE CONTRACT ITSELF NAMES CAN NEVER BE REMOVED.

    normalize_variation_values seeds `variation_values` with the contract's own
    allowed/excluded values, so the stored list mixes two very different things —
    what the CONTRACT says, and what an ADMIN taught it. Only the second kind is
    the admin's to take back. Removing the first kind is refused here rather than
    silently ignored, because a silent no-op reads as "done" in the UI.

    Worth knowing (verified in rule_compiler._enum_match_rows and _cmp_bool): the
    compiler UNIONS the contract's values with variation_values rather than letting
    the latter replace them, so emptying this list cannot disarm a rule. The guard
    above is therefore about honesty and intent, not about preventing breakage.

    Deterministic — no LLM, and no model is consulted: taking back something a
    person added needs no permission from a checker. Raises RuleEditError on any
    invalid input — safe for HTTP 400.
    """
    spec = _as_dict(rule_spec)
    if spec.get("kind") != "ir_v1" or not isinstance(spec.get("ir"), dict):
        raise RuleEditError(
            "Only AI rule-engine (IR) rules can be edited. Re-upload the contract "
            "to regenerate this rule.")

    ctx = variation_context(spec)
    if ctx is None:
        raise RuleEditError(
            "Spellings apply only to rules that match a value against the "
            "contract — an allowed/prohibited list, or a conditional value.")
    if not ctx["field"]:
        raise RuleEditError("This rule is not bound to an output column yet.")

    clean = str(spelling or "").strip()
    if not clean:
        raise RuleEditError("Pick a variation to remove.")
    key = _vnorm(clean)
    if not key:
        raise RuleEditError("Pick a variation to remove.")

    # THE guard. `base` is whatever the contract itself names for this template —
    # allowed / excluded / the conditional target (see variation_context).
    for b in ctx["base"]:
        if _vnorm(b) == key:
            raise RuleEditError(
                f"“{b}” is a value this contract names, so it cannot be removed "
                f"here. Re-upload the contract if the contract itself is wrong.")

    ir = spec["ir"]
    params = ir.get("params") or {}
    vv = params.get("variation_values")
    template = ctx["template"]

    if template in _COND_VARIATION_TEMPLATES and isinstance(vv, dict):
        # Keyed map: only the TARGET value's bucket is the admin's to edit. The
        # other keys describe the CONDITION side — deleting from those would
        # change WHEN the rule applies, not what it accepts.
        target_key = None
        want = _vnorm(ctx["value"])
        for k in vv:
            if _vnorm(k) == want:
                target_key = k
                break
        if target_key is None:
            raise RuleEditError(
                "This variation is not stored on this rule, so there is nothing "
                "to remove.")
        bucket = vv.get(target_key)
        bucket = list(bucket) if isinstance(bucket, list) else ([bucket] if bucket else [])
        kept = [x for x in bucket if _vnorm(x) != key]
        if len(kept) == len(bucket):
            raise RuleEditError(
                f"“{clean}” is not stored on this rule, so there is nothing to "
                f"remove.")
        new_vv: Any = {**vv, target_key: kept}
        after = _flat(new_vv)
    else:
        current = list(vv) if isinstance(vv, list) else []
        kept = [x for x in current if _vnorm(x) != key]
        if len(kept) == len(current):
            raise RuleEditError(
                f"“{clean}” is not stored on this rule, so there is nothing to "
                f"remove.")
        new_vv = kept
        after = list(kept)

    new_params = {**params}
    # An empty list is a live shape, but ABSENT is the shape a freshly generated
    # rule with no spellings carries — drop the key so a rule returns to exactly
    # the state it would have had, rather than to a near-miss the compiler and the
    # normalizer each have to special-case.
    if _flat(new_vv):
        new_params["variation_values"] = new_vv
    else:
        new_params.pop("variation_values", None)

    new_ir = dict(ir)
    new_ir["params"] = new_params

    ok, reason = validate_ir(new_ir, set(output_schema.field_names))
    if not ok:
        raise RuleEditError(f"Cannot remove this variation: {reason}")

    try:
        new_sql = compile_ir(new_ir, output_schema.field_to_sheets,
                             getattr(output_schema, "primary_sheet", None),
                             aliases=getattr(output_schema, "field_aliases", None))
    except CompileError as exc:
        raise RuleEditError(f"Cannot rebuild the validation query: {exc}")

    new_spec = dict(spec)
    new_spec["ir"] = new_ir
    new_spec["compiled_sql"] = new_sql
    new_spec["executable"] = True
    new_spec.pop("unmapped_reason", None)
    return new_spec, after, ctx


def _editable_bucket(spec: dict, ctx: dict) -> list:
    """The slice of `variation_values` remove_variation_value is willing to touch.

    For the enum templates that is the whole flat list. For the conditional ones,
    whose spellings live in a {value: [...]} map, it is ONLY the target value's
    bucket — the other keys spell out the CONDITION, and changing those changes
    WHEN the rule applies rather than what it accepts.
    """
    params = (spec.get("ir") or {}).get("params") or {}
    vv = params.get("variation_values")
    if ctx["template"] in _COND_VARIATION_TEMPLATES and isinstance(vv, dict):
        want = _vnorm(ctx["value"])
        for k, bucket in vv.items():
            if _vnorm(k) == want:
                return list(bucket) if isinstance(bucket, list) else (
                    [bucket] if bucket else [])
        return []
    return _flat(vv)


def removable_variations(rule_spec: Any) -> list:
    """The spellings on this rule an admin may take back — what is stored in
    `variation_values` MINUS the values the contract itself names, and for a
    conditional rule minus everything outside the target bucket.

    The UI needs this to decide which chips get a remove affordance. It is derived
    here, from the same two rules remove_variation_value enforces, so a chip can
    never offer a removal the server would then refuse — a mismatch a caller has no
    way to detect except by clicking and getting a 400.
    """
    spec = _as_dict(rule_spec)
    ctx = variation_context(spec)
    if ctx is None:
        return []
    base = {_vnorm(b) for b in ctx["base"]}
    out, seen = [], set()
    for v in _editable_bucket(spec, ctx):
        k = _vnorm(v)
        if not k or k in base or k in seen:
            continue
        seen.add(k)
        out.append(v)
    return out
