"""
vocabulary.py
─────────────
Canonical value normalization so rules bind to the real values a BDX carries.

Concern from the audit: a rule matches "US"/"CGL"/"nightclub" while the BDX
carries "USA"/free-text class codes — so even a correct rule fails to bind. The
fix is a deterministic synonym map applied to the *literals* the LLM extracts
into an IR (allowed/excluded sets and scope values) BEFORE the rule is compiled,
so the rule side is canonical.

This module is the seed of that map. It is intentionally small and is treated as
**versioned data** (see VOCAB_VERSION): editable/extensible without touching the
compiler. `VOCAB_VERSION` is stamped onto each generated rule so a normalization
change is auditable rather than silently shifting past results.

The compiler lower-cases and trims values when it builds the SQL, so canonical
tokens here are written lower-case.
"""

from __future__ import annotations

import json
import re as _re

from contract_upload_services.catalog_store import (
    get_vocab,
    get_field_class_hints,
    vocab_version as _vocab_version,
)


def _norm(value) -> str:
    """String-normalize a value (lower + strip every non-alphanumeric) — mirrors
    rule_compiler._norm_py / _norm_sql so the canonical tokens line up on both
    the rule side (Python) and the BDX-cell side (SQL)."""
    return _re.sub(r"[^a-z0-9]", "", str(value).lower())


# Loaded from the DB/seed (see catalog_store) — VERSIONED DATA, not hardcoded.
#   VOCAB             : class -> { canonical_token : [synonyms...] }  (lower-case)
#   FIELD_CLASS_HINTS : [(substring_of_field_name, class), ...]  first match wins
#   VOCAB_VERSION     : hash of the loaded vocab (stamped onto each rule)
VOCAB: dict[str, dict[str, list[str]]] = get_vocab()
FIELD_CLASS_HINTS: list = [tuple(h) for h in get_field_class_hints()]
VOCAB_VERSION = _vocab_version()


def reload_vocab():
    """Rebuild the in-memory vocab from the DB (after a catalog_store.reload())."""
    global VOCAB, FIELD_CLASS_HINTS, VOCAB_VERSION
    from contract_upload_services import catalog_store
    catalog_store.reload()
    VOCAB = get_vocab()
    FIELD_CLASS_HINTS = [tuple(h) for h in get_field_class_hints()]
    VOCAB_VERSION = _vocab_version()


def _class_for_field(field: str | None) -> str | None:
    if not field:
        return None
    fl = field.strip().lower()
    for hint, cls in FIELD_CLASS_HINTS:
        if hint in fl:
            return cls
    return None


def derive_class_for_field(field: str | None) -> tuple[str | None, str | None]:
    """(class, hint) to file a spelling for `field` under — the class its name
    already hints at, or a new one DERIVED FROM THE COLUMN NAME when no hint
    matches yet.

    Deriving one matters. Only 7 hints exist today (territory / country / region /
    coverage / cover / line of business / lob), so a carrier, paper or reinsurer
    column matches none of them — and a vocabulary that silently refuses to learn
    the columns people actually correct is a vocabulary nobody trusts.

    The hint is the column's HEAD words: everything before the first parenthesis,
    trimmed to three words. "Legal Entity (Specialty vs Cayman Paper)" and
    "Legal Entity" both become hint 'legal entity' → class 'legal_entity', so the
    same column in the next contract resolves to the same class. Registering the
    hint alongside the term is what keeps this safe: once a column is HINTED,
    _classes_for consults exactly one class for it instead of walking all of them.

    Returns (None, None) for a field name with no usable words.
    """
    if not field:
        return None, None
    existing = _class_for_field(field)
    if existing:
        return existing, None                    # already hinted — nothing to register
    head = str(field).split("(")[0]
    words = _re.findall(r"[a-z0-9]+", head.lower())
    # Drop trailing noise words that carry no identity on their own.
    while words and words[-1] in {"name", "code", "type", "value", "id"} and len(words) > 1:
        words.pop()
    if not words:
        return None, None
    words = words[:3]
    return "_".join(words), " ".join(words)


def _normalize_in_class(value, cls: str | None):
    """Map a single value to its canonical token within a class (or any class if
    cls is None). Identity (trim only) if no synonym matches."""
    if value is None:
        return value
    v = str(value).strip()
    vl = v.lower()
    classes = [cls] if cls else list(VOCAB.keys())
    for c in classes:
        table = VOCAB.get(c) or {}
        for canonical, synonyms in table.items():
            if vl == canonical or vl in synonyms:
                return canonical
    return v


def normalize_value(value, field: str | None = None):
    """Normalize one value, inferring the vocab class from the field name."""
    return _normalize_in_class(value, _class_for_field(field))


def _classes_for(field: str | None) -> list:
    """The vocab classes to consult for `field`: the class its NAME hints at, or
    EVERY class when the name matches no hint.

    Both halves of the compiler's symmetric canonicalization — canonical_token
    for the rule literal and class_form_map for the BDX cell — MUST walk this
    same ordered list. When only one of them fell back to the whole vocab, the
    literal collapsed to a canonical token the cell never collapsed to
    ('General Liability' → 'cgl' on the rule side, 'generalliability' on the cell
    side, for an unhinted field like PRODUCT_NAME), so the two could never be
    equal and the rule matched no row — silently inverting an "authorized
    classes" check into a violation on every policy."""
    cls = _class_for_field(field)
    return [cls] if cls else list(VOCAB.keys())


def canonical_token(value, field: str | None = None) -> str:
    """Canonical *string-normalized* token for a value within its field's vocab
    class (e.g. "United States of America"/"USA"/"US" → "us"). Falls back to the
    value's own string-normalized form when the class doesn't know it. This is the
    match key the compiler compares against the (identically canonicalized) cell."""
    vnorm = _norm(value)
    for c in _classes_for(field):
        for canonical, synonyms in (VOCAB.get(c) or {}).items():
            forms = {_norm(canonical)} | {_norm(s) for s in synonyms}
            if vnorm in forms:
                return _norm(canonical)
    return vnorm


def class_form_map(field: str | None) -> dict[str, str]:
    """{normalized_form: normalized_canonical} for EVERY form (canonical +
    synonyms) in the classes that apply to `field` — used to vocab-normalize the
    BDX cell in SQL so both sides collapse to the same token.

    Walks _classes_for(field) and resolves each form FIRST-WINS, exactly as
    canonical_token does, so a form carried by two classes canonicalizes the same
    way on the cell side as on the rule side."""
    out: dict[str, str] = {}
    for c in _classes_for(field):
        for canonical, synonyms in (VOCAB.get(c) or {}).items():
            canon = _norm(canonical)
            for form in [canonical, *synonyms]:
                fn = _norm(form)
                if fn:
                    out.setdefault(fn, canon)
    return out


def synonyms_for_value(value, field: str | None = None) -> list[str]:
    """Every surface form the shared vocabulary already knows for `value` (its
    canonical spelling plus all recorded synonyms), minus `value` itself.

    This is the READ side of the loop: a spelling one tenant_admin corrected once
    is offered back to every rule generated afterwards that names the same value,
    so the same correction is never needed twice. Returns [] when the vocabulary
    has never seen the value.
    """
    vnorm = _norm(value)
    if not vnorm:
        return []
    out: list[str] = []
    for c in _classes_for(field):
        for canonical, synonyms in (VOCAB.get(c) or {}).items():
            forms = [canonical, *(synonyms or [])]
            if vnorm in {_norm(f) for f in forms}:
                out.extend(forms)
    seen, uniq = {vnorm}, []
    for f in out:
        k = _norm(f)
        if k and k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def add_admin_synonym(canonical, synonym, *, field: str | None = None,
                      created_by=None, source_rule_id=None,
                      source_tenant_id=None) -> dict | None:
    """Record `synonym` as another way of writing `canonical` in the SHARED
    vocabulary, and return {'class', 'canonical', 'synonym', 'hint'} — or None
    when the value could not be filed under any class.

    The vocabulary is deliberately GLOBAL: every tenant reads the same table, so a
    spelling corrected once by one tenant_admin is understood for everyone
    afterwards. That is the point of the feature — and the reason each row also
    carries provenance (who, which rule, which tenant) once migration 17 is
    applied. The write is skipped entirely, rather than guessed at, when the
    column name yields no usable class.

    Both the canonical and the synonym are stored LOWER-CASE: _normalize_in_class
    compares a lower-cased value directly against these strings, so a stored
    capital would simply never match.

    Serialises with json.dumps and never stores caller text raw —
    catalog_store._load_from_db json.loads() this column and is fail-loud, so a
    non-array blob would take down rule generation for the whole process.
    """
    from sqlalchemy.sql import text as _text
    from db import canonical_engine

    canon = str(canonical or "").strip().lower()
    syn = str(synonym or "").strip().lower()
    if not canon or not syn or _norm(syn) == _norm(canon):
        return None

    cls, hint = derive_class_for_field(field)
    if not cls:
        return None

    with canonical_engine.begin() as conn:
        cols = {r[0] for r in conn.execute(_text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'vocabulary_term'")).fetchall()}

        row = conn.execute(_text(
            "SELECT synonyms_json FROM vocabulary_term "
            "WHERE class = :c AND canonical = :k"),
            {"c": cls, "k": canon}).first()

        if row is None:
            existing = []
        else:
            try:
                existing = json.loads(row[0] or "[]")
            except Exception:
                existing = []
            if not isinstance(existing, list):
                existing = []

        have = {_norm(s) for s in existing}
        if _norm(syn) in have:
            merged = existing                      # already known — keep the row as-is
        else:
            merged = [*existing, syn]

        payload = json.dumps(merged)
        if row is None:
            # Only name provenance columns that actually exist, so the feature
            # works before migration 17 is applied as well as after.
            extra = {"source": "'admin'", "created_by": ":cb",
                     "source_rule_id": ":srid", "source_tenant_id": ":stid"}
            names, values = [], []
            for col, placeholder in extra.items():
                if col in cols:
                    names.append(col)
                    values.append(placeholder)
            conn.execute(_text(
                f"INSERT INTO vocabulary_term (class, canonical, synonyms_json"
                f"{''.join(', ' + n for n in names)}) "
                f"VALUES (:c, :k, :s{''.join(', ' + v for v in values)})"),
                {"c": cls, "k": canon, "s": payload, "cb": created_by,
                 "srid": source_rule_id, "stid": source_tenant_id})
        elif merged is not existing:
            sets = "synonyms_json = :s"
            if "updated_at" in cols:
                sets += ", updated_at = now()"
            if "source" in cols:
                # The row now carries content that exists in no seed file. Marking
                # it 'admin' is what makes seed_rule_catalog.py leave it alone —
                # both on --reset and on its ordinary upsert, which would otherwise
                # replace the whole synonyms list and drop this addition silently.
                sets += ", source = 'admin'"
            conn.execute(_text(
                f"UPDATE vocabulary_term SET {sets} "
                f"WHERE class = :c AND canonical = :k"),
                {"s": payload, "c": cls, "k": canon})

        # Register the column→class hint so this column resolves to exactly THIS
        # class next time instead of falling back to every class in the vocabulary.
        if hint:
            hcols = {r[0] for r in conn.execute(_text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'vocabulary_field_hint'")).fetchall()}
            hint_extra = ", source" if "source" in hcols else ""
            hint_value = ", 'admin'" if "source" in hcols else ""
            conn.execute(_text(
                f"INSERT INTO vocabulary_field_hint (hint, class, sort_order{hint_extra}) "
                f"VALUES (:h, :c, 100{hint_value}) ON CONFLICT (hint) DO NOTHING"),
                {"h": hint, "c": cls})

    return {"class": cls, "canonical": canon, "synonym": syn, "hint": hint}


def remove_admin_synonym(canonical, synonym, *, field: str | None = None,
                         source_rule_id=None) -> dict | None:
    """Take back ONE add_admin_synonym write. Returns {'class','canonical',
    'synonym','row_deleted'} when something was removed, else None.

    Deliberately hard to trigger, because this table is GLOBAL. Every tenant reads
    it, so deleting the wrong row silently changes how other people's contracts
    match — the exact failure mode the whole variation feature is built to avoid.
    A synonym is therefore removed ONLY when the row proves it was this rule's own
    admin addition:

        source = 'admin'  AND  source_rule_id = <this rule>

    Both conditions come from migration 17. Where that migration has NOT been
    applied the provenance cannot be established at all, so nothing is removed and
    None is returned — the caller reports that plainly rather than deleting a row
    it cannot prove ownership of. A seeded row is never touched under any
    circumstances.

    The row itself is dropped only when the removed synonym was its last one;
    otherwise the entry stays and just loses that one spelling.
    """
    from sqlalchemy.sql import text as _text
    from db import canonical_engine

    canon = str(canonical or "").strip().lower()
    syn = str(synonym or "").strip().lower()
    if not canon or not syn:
        return None

    cls, _hint = derive_class_for_field(field)
    if not cls:
        return None

    with canonical_engine.begin() as conn:
        cols = {r[0] for r in conn.execute(_text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'vocabulary_term'")).fetchall()}
        if not {"source", "source_rule_id"} <= cols:
            return None            # cannot prove ownership -> never guess

        row = conn.execute(_text(
            "SELECT synonyms_json FROM vocabulary_term "
            "WHERE class = :c AND canonical = :k AND source = 'admin' "
            "  AND source_rule_id = :rid"),
            {"c": cls, "k": canon, "rid": source_rule_id}).first()
        if row is None:
            return None

        try:
            existing = json.loads(row[0] or "[]")
        except Exception:
            existing = []
        if not isinstance(existing, list):
            existing = []

        kept = [s for s in existing if _norm(s) != _norm(syn)]
        if len(kept) == len(existing):
            return None            # this row does not carry that spelling

        if kept:
            sets = "synonyms_json = :s"
            if "updated_at" in cols:
                sets += ", updated_at = now()"
            conn.execute(_text(
                f"UPDATE vocabulary_term SET {sets} "
                f"WHERE class = :c AND canonical = :k AND source = 'admin' "
                f"  AND source_rule_id = :rid"),
                {"s": json.dumps(kept), "c": cls, "k": canon, "rid": source_rule_id})
            deleted = False
        else:
            # Last spelling gone: the entry now says nothing. Scoped to 'admin' so
            # a seeded row can never be reached by this path.
            conn.execute(_text(
                "DELETE FROM vocabulary_term "
                "WHERE class = :c AND canonical = :k AND source = 'admin' "
                "  AND source_rule_id = :rid"),
                {"c": cls, "k": canon, "rid": source_rule_id})
            deleted = True

    return {"class": cls, "canonical": canon, "synonym": syn, "row_deleted": deleted}


def normalize_ir_literals(ir: dict) -> dict:
    """Return a copy of the IR with its SCOPE literals canonicalized.

    Only scope values are canonicalized here. `allowed`/`excluded` are left in
    their ORIGINAL contract wording (e.g. "United States of America", not "us")
    so the rule and its message read properly — the compiler canonicalizes BOTH
    the rule value and the BDX cell symmetrically at SQL-build time
    (vocabulary.canonical_token / class_form_map), so binding still works
    regardless of how the sheet spells the value.

    Touches only value literals — never field names or numeric params.
    """
    if not isinstance(ir, dict):
        return ir
    params = ir.get("params")
    if not isinstance(params, dict):
        return ir

    new_params = dict(params)

    scope = new_params.get("scope")
    if isinstance(scope, dict):
        # scope is {field_name: value}; infer the class from each scope field. A
        # value may be a LIST (a grouped IN-scope) — normalize each element — or an
        # OPERATOR-OBJECT {op, value} — normalize only the inner value and KEEP the
        # dict (a plain str() would corrupt it into a literal like "{'op':'='...}"
        # that the compiler then matches against, flagging nothing).
        def _norm_scope(v, k):
            if isinstance(v, (list, tuple)):
                return [normalize_value(x, k) for x in v]
            if isinstance(v, dict):
                out = dict(v)
                if "value" in out:
                    iv = out["value"]
                    out["value"] = ([normalize_value(x, k) for x in iv]
                                    if isinstance(iv, (list, tuple))
                                    else normalize_value(iv, k))
                return out
            return normalize_value(v, k)
        new_params["scope"] = {
            k: _norm_scope(v, k) for k, v in scope.items()
        }

    new_ir = dict(ir)
    new_ir["params"] = new_params
    return new_ir
