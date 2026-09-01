import os
import re
import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

import pipeline_log as plog
from contract_upload_services.document_extractors import extract_document_data
from contract_upload_services.validation_rule_generator import ValidationRuleGenerator


# Bundle-level knobs. Defaults reproduce today's behaviour:
#   CONCURRENCY=1 → contracts processed serially (as before).
#   Raise it (e.g. 3-4) to process independent contracts in parallel; the shared
#   AI gateway rate-limiter (gemini_service) provides the real backpressure, so
#   the pool size is a ceiling, not a quota risk.
_CONTRACT_CONCURRENCY = int(os.getenv("KAVACHIO_CONTRACT_CONCURRENCY", "1"))
# Per-contract wall-clock timeout (seconds) when running in parallel; 0 = off.
_CONTRACT_TIMEOUT = int(os.getenv("KAVACHIO_CONTRACT_TIMEOUT", "0"))


class ContractExtractionService:

    def __init__(self):

        self.validation_generator = ValidationRuleGenerator()

    # =====================================================
    # PROCESS SINGLE CONTRACT
    # =====================================================

    def process_contract(self, file_path, output_dir=None, template_fields=None,
                         halt_on_external_references=False, resume_token=None,
                         reference_documents=None, tenant_id=None):
        """Extract contract data and generate validation rules.

        Args:
            file_path: Path to the contract file.
            tenant_id: Owning tenant of this contract. Scopes the generic rule
                library so this upload gets the platform's global rules plus
                THIS tenant's own rules, and no other tenant's.
            output_dir: Optional directory for debug output JSON.
            template_fields: Optional list of Output Template field dicts
                [{name, sheet, canonical_field, samples}].
                When provided, rule synthesis targets Output Template field
                names instead of the canonical data model — implementing the
                Contract → Output Template → Data Model hierarchy.
            halt_on_external_references: When True, stop before rule generation
                if the contract defers rules to external documents, returning a
                {"halted_for_references": True, "external_references": [...],
                "resume_token": ...} dict so the UI can prompt the user.
            resume_token: When set, resume a previously halted run from its cached
                extraction — skips both the document parse and the extraction LLM
                call (used by "Continue Anyway").
        """
        print(f"\nProcessing Contract: {file_path}")
        # One header per contract so concurrent uploads stay separable in the file.
        plog.new_run(f"contract={os.path.basename(file_path)} "
                     f"template_fields={len(template_fields or [])}")

        # -------------------------------------------------
        # STEP 1 — Extract document data
        # (skipped when resuming — the cached extraction is reused downstream)
        # -------------------------------------------------

        if resume_token:
            print("\n[Process] RESUME — using cached extraction; skipping document parse.")
            extracted_data = {"pages": []}
        else:
            print("\nExtracting document data...")
            # PDF parse + OCR. No model call — worth timing precisely because it is
            # the one slow step that is NOT the model, so it tells you whether a slow
            # upload is the network or the document.
            with plog.stage("PDF parse (no AI)"):
                extracted_data = extract_document_data(file_path)

        # -------------------------------------------------
        # STEP 2 — Generate validation rules
        # (template-aware when template_fields provided)
        # -------------------------------------------------

        print("Generating validation rules...")

        # Context caches are billed for STORAGE while they live, so this contract's
        # caches are released in the `finally` below no matter how it ends. They are
        # keyed by prefix content, so a NEXT contract against the same template
        # simply re-creates one — correctness never depends on them surviving.
        validation_rules = (
            self.validation_generator.generate_validation_rules_json(
                extracted_data,
                file_path,
                output_dir=output_dir,
                template_fields=template_fields,
                halt_on_external_references=halt_on_external_references,
                resume_token=resume_token,
                reference_documents=reference_documents,
                tenant_id=tenant_id,
            )
        )

        try:
            from contract_upload_services.gemini_service import release_context_caches
            release_context_caches()
        except Exception as _exc:
            print(f"[ctx-cache] release skipped: {_exc}")

        # Timing summary closes the run's block in pipeline_decisions.log.
        plog.finish_run()

        # Halted before rule generation — return the partial payload as-is so
        # the route can prompt the user about the referenced documents.
        if isinstance(validation_rules, dict) and validation_rules.get("halted_for_references"):
            print("\n[Process] HALTED for external references — skipping rule generation.\n")
            return validation_rules

        # -------------------------------------------------
        # Print the final extraction output JSON
        # -------------------------------------------------
        print("\n" + "=" * 60)
        print("FINAL EXTRACTION OUTPUT")
        print("=" * 60)
        print(json.dumps(validation_rules, indent=2, default=str))
        print("=" * 60 + "\n")

        return validation_rules

    # =====================================================
    # PROCESS MULTIPLE CONTRACTS
    # =====================================================

    @staticmethod
    def _source_sig(file_path):
        """Change-key for a source file (size + mtime) used to decide whether a
        checkpointed result is still valid."""
        st = os.stat(file_path)
        return {"size": st.st_size, "mtime": int(st.st_mtime)}

    def _process_one_file(self, file_path, output_dir):
        """Process ONE contract to its rules JSON. Never raises — returns a
        summary dict (status ok/error) so one bad contract cannot sink the bundle."""
        try:
            output = self.process_contract(file_path, output_dir=output_dir)

            safe_name = re.sub(
                r"[^a-zA-Z0-9_]", "_",
                os.path.splitext(os.path.basename(file_path))[0])
            out_file = os.path.join(output_dir, f"{safe_name}_v_rules.json")

            with open(out_file, "w") as f:
                json.dump(output, f, indent=2, default=str)

            meta = output.get("metadata", {}) or {}
            return {
                "file": os.path.basename(file_path),
                "status": "ok",
                "output": out_file,
                "rule_count": meta.get("contract_rule_count", 0),
                "validation_rule_count": meta.get("validation_rule_count", 0),
                "stage_a": meta.get("stage_a_summary", {}) or {},
                "stage_b": meta.get("stage_b_summary", {}) or {},
            }
        except Exception as e:
            print(f"\n[ERROR] {os.path.basename(file_path)}\n{e}")
            return {
                "file": os.path.basename(file_path),
                "status": "error",
                "error": str(e),
            }

    def process_contracts(
        self,
        contract_files,
        output_dir="validation_output_rules",
        resume=True,
    ):
        """Process a bundle of contracts with per-contract isolation, an on-disk
        checkpoint (resume after a crash / skip unchanged files), and optional
        bounded parallelism (KAVACHIO_CONTRACT_CONCURRENCY). Defaults reproduce
        the previous serial behaviour, plus resume."""

        os.makedirs(output_dir, exist_ok=True)

        ckpt_path = os.path.join(output_dir, "_bundle_checkpoint.json")
        lock = threading.Lock()

        checkpoint = {}
        if resume and os.path.exists(ckpt_path):
            try:
                with open(ckpt_path) as f:
                    checkpoint = json.load(f)
            except Exception:
                checkpoint = {}

        def _save_ckpt():
            with lock:
                try:
                    with open(ckpt_path, "w") as f:
                        json.dump(checkpoint, f, indent=2, default=str)
                except Exception as ex:
                    print(f"[checkpoint] save failed: {ex}")

        def _handle(idx, file_path):
            base = os.path.basename(file_path)
            print(f"\n{'#' * 60}\nProcessing: {base}\n{'#' * 60}")

            if not os.path.exists(file_path):
                print(f"[SKIP] File not found: {file_path}")
                return idx, {"file": base, "status": "missing"}

            sig = self._source_sig(file_path)
            done = checkpoint.get(base)
            # Resume/skip: identical file already processed OK and its output
            # still on disk → reuse with ZERO AI calls.
            if (resume and done and done.get("status") == "ok"
                    and done.get("_sig") == sig
                    and os.path.exists(done.get("output", ""))):
                print(f"[RESUME] {base} unchanged & already done — skipping (0 LLM calls).")
                return idx, {k: v for k, v in done.items() if k != "_sig"}

            s = self._process_one_file(file_path, output_dir)

            # Persist this contract's result immediately so a crash mid-bundle
            # resumes from here instead of restarting.
            with lock:
                checkpoint[base] = {**s, "_sig": sig}
            _save_ckpt()
            return idx, s

        indexed = list(enumerate(contract_files))
        summary_slots = [None] * len(indexed)

        if _CONTRACT_CONCURRENCY > 1 and len(indexed) > 1:
            print(f"[bundle] processing {len(indexed)} contracts with "
                  f"concurrency={_CONTRACT_CONCURRENCY}")
            with ThreadPoolExecutor(max_workers=_CONTRACT_CONCURRENCY) as ex:
                fut_map = {ex.submit(_handle, i, fp): (i, fp) for i, fp in indexed}
                for fut in list(fut_map):
                    i, fp = fut_map[fut]
                    try:
                        idx, s = fut.result(
                            timeout=_CONTRACT_TIMEOUT or None)
                    except _FuturesTimeout:
                        idx = i
                        s = {"file": os.path.basename(fp), "status": "error",
                             "error": f"timed out after {_CONTRACT_TIMEOUT}s"}
                        print(f"[TIMEOUT] {os.path.basename(fp)}")
                    summary_slots[idx] = s
        else:
            for i, fp in indexed:
                idx, s = _handle(i, fp)
                summary_slots[idx] = s

        summary = [s for s in summary_slots if s is not None]

        # Per-contract console recap (kept from the original for parity).
        for item in summary:
            if item["status"] == "ok":
                sa = item.get("stage_a", {}) or {}
                sb = item.get("stage_b", {}) or {}
                print(f"\n✓ Saved : {item.get('output')}")
                print(f"  contract_rules   : {item.get('rule_count', 0)}")
                print(f"  validation_rules : {item.get('validation_rule_count', 0)}")
                print(f"  stage_a          : bearing={sa.get('rule_bearing', 0)}, "
                      f"not_bearing={sa.get('not_rule_bearing', 0)}, "
                      f"errors={sa.get('errors', 0)}")
                print(f"  stage_b          : ajv={sb.get('ajv_rules', 0)}, "
                      f"custom={sb.get('custom_rules', 0)}, active={sb.get('active', 0)}, "
                      f"needs_review={sb.get('needs_review', 0)}, dropped={sb.get('dropped', 0)}")

        # =====================================================
        # FINAL SUMMARY
        # =====================================================

        print(f"\n{'=' * 60}")
        print("SUMMARY")
        print(f"{'=' * 60}")

        for item in summary:

            if item["status"] == "ok":

                print(f"\n✓ {item['file']}")
                print(f"  Rules  : {item['rule_count']}")
                print(f"  Output : {item['output']}")

            elif item["status"] == "missing":

                print(f"\n⚠ {item['file']} → FILE NOT FOUND")

            else:

                print(f"\n✗ {item['file']}")
                print(f"  ERROR : {item['error']}")

        return summary


# # =========================================================
# # ENTRY POINT
# # =========================================================

# if __name__ == "__main__":

#     CONTRACT_FILES = [
#         "/Users/at-mac10/Documents/Sachin/Kavachio/Documents/send/Sample Contracts & BDX/Contract - Aurenity - Schedule A - 2025.pdf",
#     ]

#     OUTPUT_DIR = "validation_output_rules"

#     service = ContractExtractionService()

#     service.process_contracts(
#         contract_files=CONTRACT_FILES,
#         output_dir=OUTPUT_DIR
#     )