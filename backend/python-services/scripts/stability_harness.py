"""
stability_harness.py
────────────────────
Phase-3 stability harness: run the offline rule-generation pipeline N times
against the SAME contract + template and measure what drifts.

The 935/937 audit showed one regeneration pair drifting on 13 of 48 rules.
This harness turns that one-off audit into a repeatable measurement: every
rule is fingerprinted by WHAT IT DOES (regen_reconcile's kind + fields +
canonical params — never the LLM-worded name), runs are diffed pairwise, and
the per-rule stability table shows exactly which checks are settled and which
the model keeps re-deciding. Run it before and after a pipeline change to see
whether the change helped.

Usage (backend venv, from backend/python-services):

    python scripts/stability_harness.py --pdf /path/to/contract.pdf \
        --template-id 828 --runs 5 [--out stability_report.json]

    # or reuse a template's fields from a prior contract:
    python scripts/stability_harness.py --pdf contract.pdf \
        --fields-from-contract 937 --runs 5

Environment: needs DATABASE_URL (template fields / library rules) and the
Gemini key, exactly like the server. Each run costs real model calls — the
answer caches are DISABLED for the run (KAVACHIO_AI_CACHE=0) because measuring
stability through a cache would measure the cache, not the model. Context
(prefix) caching stays ON: it changes billing, never tokens.

No DB writes: generation output is kept in memory and diffed; nothing is
persisted.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Measure the MODEL, not the caches: a generic-bind cache hit would make every
# run after the first identical by construction.
os.environ.setdefault("KAVACHIO_AI_CACHE", "0")
# ...and never let a prior identical upload short-circuit rule generation.
os.environ.setdefault("KAVACHIO_DISABLE_CONTRACT_REUSE", "1")
# The harness compares raw generation runs — carry-forward would mask drift.
os.environ.setdefault("KAVACHIO_CARRY_FORWARD", "0")


def _template_fields_from_contract(contract_id):
    """Rebuild the template_fields list the way the upload route does, from the
    export template the contract is bound to."""
    from sqlalchemy import text
    from db import canonical_engine
    with canonical_engine.connect() as conn:
        tpl_id = conn.execute(
            text("SELECT output_template_id FROM contract WHERE contract_id = :c"),
            {"c": contract_id}).scalar()
    if not tpl_id:
        raise SystemExit(f"contract {contract_id} has no output_template_id")
    return _template_fields_from_template(tpl_id)


def _template_fields_from_template(template_id):
    from db import SessionLocal, ExportTemplate
    from app_routes import _template_fields_from_structure
    with SessionLocal() as s:
        tmpl = s.get(ExportTemplate, template_id)
        if not tmpl:
            raise SystemExit(f"export template {template_id} not found")
        return _template_fields_from_structure(tmpl.structure)


def _one_run(pdf_path, template_fields, run_no):
    from contract_upload_services.contract_extraction_service import (
        ContractExtractionService,
    )
    print(f"\n──────── RUN {run_no} ────────")
    svc = ContractExtractionService()
    out = svc.process_contract(
        pdf_path,
        template_fields=template_fields,
        halt_on_external_references=False,
    )
    rules = (out or {}).get("validation_rules") or []
    review = (out or {}).get("review_queue") or []
    return rules, review


def _fingerprint(rule):
    from contract_upload_services.regen_reconcile import (
        rule_kind, target_fields, core_params, is_library_rule,
    )
    return {
        "identity": (rule_kind(rule), target_fields(rule)),
        "core": core_params(rule),
        "library": is_library_rule(rule),
        "name": rule.get("rule_name"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--template-id", type=int)
    ap.add_argument("--fields-from-contract", type=int)
    ap.add_argument("--out", default="stability_report.json")
    args = ap.parse_args()

    if args.template_id:
        template_fields = _template_fields_from_template(args.template_id)
    elif args.fields_from_contract:
        template_fields = _template_fields_from_contract(args.fields_from_contract)
    else:
        raise SystemExit("pass --template-id or --fields-from-contract")
    print(f"{len(template_fields)} template field(s); {args.runs} run(s)")

    runs = []
    for i in range(1, args.runs + 1):
        rules, review = _one_run(args.pdf, template_fields, i)
        runs.append({
            "rules": [_fingerprint(r) for r in rules],
            "n_rules": len(rules), "n_review": len(review),
        })
        print(f"RUN {i}: {len(rules)} rule(s), {len(review)} review item(s)")

    # ── Stability table: how often does each check appear, and how often with
    # identical parameters? Keyed by (kind, fields) — the check's identity. ──
    by_identity = collections.defaultdict(lambda: {
        "seen_in_runs": 0, "cores": collections.Counter(), "names": set(),
        "library": False})
    for run in runs:
        seen = set()
        for fp in run["rules"]:
            key = json.dumps(fp["identity"], default=str)
            e = by_identity[key]
            if key not in seen:
                e["seen_in_runs"] += 1
                seen.add(key)
            e["cores"][fp["core"]] += 1
            e["names"].add(fp["name"])
            e["library"] = e["library"] or fp["library"]

    n = len(runs)
    table = []
    for key, e in by_identity.items():
        top_core = e["cores"].most_common(1)[0][1] if e["cores"] else 0
        table.append({
            "identity": json.loads(key),
            "source": "library" if e["library"] else "contract",
            "presence": f"{e['seen_in_runs']}/{n}",
            "stable": e["seen_in_runs"] == n and len(e["cores"]) == 1,
            "param_variants": len(e["cores"]),
            "dominant_variant_share": round(top_core / max(1, sum(e["cores"].values())), 2),
            "names_seen": sorted(x for x in e["names"] if x),
        })
    table.sort(key=lambda t: (t["stable"], t["presence"]))

    stable = sum(1 for t in table if t["stable"])
    report = {
        "pdf": os.path.basename(args.pdf), "runs": n,
        "rule_counts": [r["n_rules"] for r in runs],
        "distinct_checks": len(table),
        "fully_stable": stable,
        "stability_pct": round(100 * stable / max(1, len(table)), 1),
        "table": table,
    }
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n════ STABILITY: {stable}/{len(table)} checks fully stable "
          f"({report['stability_pct']}%) across {n} runs ════")
    for t in table:
        if not t["stable"]:
            print(f"  UNSTABLE {t['presence']:>5}  {t['param_variants']} variant(s)  "
                  f"{t['source']:<8} {(t['names_seen'] or ['?'])[0]}")
    print(f"full report: {args.out}")


if __name__ == "__main__":
    main()
