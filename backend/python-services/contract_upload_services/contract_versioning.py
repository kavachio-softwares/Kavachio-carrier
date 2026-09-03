"""
contract_versioning.py
──────────────────────
Exact re-upload reuse for the contract pipeline (no schema migration — the
`contract` table already carries the Type-2 SCD columns:
content_fingerprint, entity_fingerprint, is_current_version, valid_from/until).

Two helpers used by the upload handler BEFORE any LLM call:

  compute_content_fingerprint(file_bytes, template_fields, output_template_id)
      → deterministic hash of the uploaded document + the output-template shape.
        Same file + same template ⇒ same fingerprint.

  find_reusable_contract(program_id, content_fingerprint)
      → the most recent non-failed contract in the program whose
        content_fingerprint matches, or None. A hit means an identical
        re-upload, so the whole rule set can be reused and every LLM call skipped.

Determinism note: this is what stops "re-uploading the same contract produces a
different rule set each run" for the common case — the model is never called.
"""

import json
import hashlib

from sqlalchemy import text

from db import canonical_engine


def _normalize_text(text):
    """Lowercase + collapse all whitespace runs to a single space.

    This makes the fingerprint robust to trivial formatting differences
    (re-exports, line breaks, spacing) while staying sensitive to the actual
    contract wording — so the same contract content matches even if the PDF
    bytes differ between uploads.
    """
    return " ".join((text or "").split()).lower()


def compute_content_fingerprint(document_text, template_fields, output_template_id=None):
    """SHA-256 over the NORMALIZED document text + the template's FIELD NAMES.

    Uses normalized extracted text (not raw bytes) so a re-export of the same
    contract still matches. The template is folded in by its sorted field-name
    set — the actual content that drives rule generation — NOT by
    output_template_id: re-creating the same template yields a new row id but the
    same fields, and that must still count as the same work (so reuse fires).
    `output_template_id` is accepted for call compatibility but intentionally
    excluded from the hash.
    """
    h = hashlib.sha256()
    h.update(_normalize_text(document_text).encode("utf-8"))

    field_names = sorted(
        (f.get("name") or "") for f in (template_fields or [])
    )
    signature = json.dumps({"fields": field_names}, sort_keys=True)
    h.update(b"\x00")
    h.update(signature.encode("utf-8"))

    return h.hexdigest()


def compute_entity_fingerprint(program_id, filename):
    """Stable identity of a contract 'slot' — same program + filename across
    re-uploads is the same logical entity (used to chain SCD versions)."""
    key = json.dumps(
        {"program_id": program_id, "filename": (filename or "").strip().lower()},
        sort_keys=True,
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def find_reusable_contract(program_id, content_fingerprint):
    """Return {'contract_id': int} for an identical prior upload, else None.

    Matches on (program_id, content_fingerprint) and ignores failed contracts.
    Picks the most recent match so reuse follows the latest known-good version.
    """
    if not program_id or not content_fingerprint:
        return None

    with canonical_engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT contract_id
                FROM   contract
                WHERE  contract_program_id = :pid
                  AND  row_hash = :fp
                  AND  COALESCE(status_ops, '') <> 'failed'
                ORDER BY contract_id DESC
                LIMIT 1
            """),
            {"pid": program_id, "fp": content_fingerprint},
        ).first()

    if row is None:
        return None

    return {"contract_id": row[0]}
