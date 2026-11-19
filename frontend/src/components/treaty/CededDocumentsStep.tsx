/**
 * The Documents step of a reinsurance bordereau setup — the broker setup's
 * Documents tab, for a treaty: the files it is built from, as the same
 * upload boxes.
 *
 *   Input Template   REQUIRED: a sample of the carrier's own input file — or,
 *                    left empty, the broker bordereau templates already set
 *                    up on the programmes the treaty protects (one at least).
 *   Output Template  REQUIRED: the layout the reinsurers want — uploaded, or
 *                    created ("Create BDX Template", as the broker's box):
 *                    from the treaty's terms, or the Lloyd's risk sheet.
 *   Treaty           the contract it is set up for, and its SIGNED wording —
 *                    required: a treaty is configured from it (26.3).
 *
 * Each upload is read straight away (columns matched by the same AI pass a
 * broker's sample gets) and shows on the Input / Output / Field Mapping
 * steps. Nothing is saved until the setup is.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { FileSpreadsheet, FileUp, Loader2, ShieldCheck } from "lucide-react";
import { FileDrop } from "../ui/FileDrop";
import CreateCededTemplate from "./CreateCededTemplate";
import { showError } from "../ResultPopup";
import {
  uploadInputSample, uploadLayout, type SetupRecord, type SetupValues,
} from "../../api/ceded";

/** A file already read — shown in the box by its name. */
const named = (name?: string | null) => (name ? new File([], name) : null);

export default function CededDocumentsStep({ cid, rec, v, change, onOutputStep }: {
  cid: number; rec: SetupRecord; v: SetupValues;
  change: (patch: Partial<SetupValues>) => void;
  /** Opens the Output BDX Template step — where a created sheet is reviewed. */
  onOutputStep?: () => void;
}) {
  const [busy, setBusy] = useState<"" | "input" | "output">("");
  // "Create BDX Template": the two ways to create one, as the broker's.
  const [creating, setCreating] = useState(false);
  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  const outSrc = v.layout?.source ?? "kavachio";
  // Required, as a broker's are. The input is met by an upload, or by the
  // broker templates on the programmes read; the output by an upload or a
  // sheet built on the Output step.
  const brokerTemplates = rec.options.inputs.filter(t => !t.own
    && (v.program_ids == null || t.program_id == null || v.program_ids.includes(t.program_id)));
  const inputMet = !!own?.columns.length || (!own && brokerTemplates.length > 0);
  const outputMet = v.columns.length > 0;

  async function pickInput(f: File | null) {
    if (!f) { change({ input_layout: { source: "brokers" } }); return; }
    setBusy("input");
    try {
      change({ input_layout: await uploadInputSample(cid, f) });
    } catch (e) {
      showError(e);
    } finally {
      setBusy("");
    }
  }

  async function pickOutput(f: File | null) {
    if (!f) {
      // Removed — upload another, or create one.
      change({ layout: { source: "contract" }, columns: [], layout_file: null, file_format: "xlsx" });
      return;
    }
    setBusy("output");
    try {
      const p = await uploadLayout(cid, f);
      change({ layout: p.layout, columns: p.columns, layout_file: p.layout_file,
               file_format: p.file_format });
    } catch (e) {
      showError(e);
    } finally {
      setBusy("");
    }
  }


  // A created sheet fills the box as an upload does, by what it was made from.
  const outputShown = busy === "output" ? null
    : outSrc === "uploaded" ? named(v.layout?.filename)
    : outputMet && outSrc === "contract" ? named("Created from the treaty's terms")
    : outputMet && outSrc === "standard" ? named("Created from the Lloyd's risk sheet")
    : null;

  return (
    <div>
      <CreateCededTemplate open={creating} onClose={() => setCreating(false)}
        cid={cid} rec={rec} v={v}
        onCreated={t => { setCreating(false); change(t); onOutputStep?.(); }} />
      <p className="sub" style={{ marginTop: 0, marginBottom: 12 }}>
        The files this treaty&rsquo;s reinsurance bordereau is built from. Both
        templates are required. Upload your own input file, or leave it empty to
        use the broker bordereau templates already set up on the programmes this
        treaty protects. Upload the output you send your reinsurers, or create one.
      </p>

      {/* The upload boxes are the broker setup's, which are styled for the
          app shell rather than .proto — so they sit outside it. */}
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4"
           style={{ fontFamily: "inherit" }}>
        <div>
          <FileDrop label="Input Template" icon={<FileUp size={15} />} required tone="required"
            file={busy === "input" ? null : named(own?.filename)}
            onPick={pickInput} accept=".xlsx,.xlsm,.csv"
            hint="Your own input file — or leave empty to use your brokers' bordereau templates"
            disabled={busy === "input"} disabledNote="Reading your columns…" />
          <div className="sub" style={{ marginTop: 6, ...(inputMet || busy ? {} : { color: "var(--p-warn-ink)" }) }}>
            {busy === "input" ? <><Loader2 size={11} className="animate-spin" /> Reading…</>
              : own ? `${new Set(own.columns.map(c => c.name)).size} columns — read each period from your file`
              : brokerTemplates.length
                ? `Your brokers' bordereau template${brokerTemplates.length === 1 ? "" : "s"} — `
                  + brokerTemplates.map(t => t.programme ?? t.name).join(", ")
                : "Required — no programme this treaty protects has a broker bordereau template yet. Upload your own input file, or set up the broker's bordereau first."}
          </div>
        </div>
        <div>
          <FileDrop label="Output Template" icon={<FileSpreadsheet size={15} />} required tone="required"
            file={outputShown}
            onPick={pickOutput} accept=".xlsx,.xlsm,.csv"
            hint="Upload the layout your reinsurers want, or build one here"
            altAction={{ label: "Create BDX Template", onClick: () => setCreating(true),
                         hint: "From the treaty's terms or the Lloyd's risk sheet" }}
            disabled={busy === "output"} disabledNote="Reading the layout…" />
          <div className="sub" style={{ marginTop: 6, ...(outputMet || busy ? {} : { color: "var(--p-warn-ink)" }) }}>
            {busy === "output" ? <><Loader2 size={11} className="animate-spin" /> Reading…</>
              : !outputMet ? (outSrc === "standard"
                  ? "Pick the territory on the Output step to build it"
                  : "Required — upload yours, or create one")
              : outSrc === "uploaded" ? `${v.columns.length} columns, filled in as they are`
              : outSrc === "standard" ? "Built from the Lloyd's risk sheet"
              : outSrc === "contract" ? `Built from the treaty's terms — ${v.columns.length} columns; change them on the Output step`
              : "Kavachio's standard layout (no longer offered) — choose another on the Output step"}
          </div>
        </div>
        <div className="sm:col-span-2 xl:col-span-1">
          <div style={{ border: "1.5px solid var(--p-border-2)", borderRadius: 10,
                        padding: "16px 14px", textAlign: "center", height: "100%" }}>
            <div style={{ display: "flex", gap: 6, justifyContent: "center", alignItems: "center",
                          fontWeight: 600, fontSize: 13 }}>
              <ShieldCheck size={15} /> Signed treaty wording
              {rec.wording
                ? <span className="badge b-ok" style={{ fontSize: 9.5 }}>On file</span>
                : <span className="badge b-warn" style={{ fontSize: 9.5 }}>Required</span>}
            </div>
            <div style={{ marginTop: 6, fontSize: 12.5 }}>
              <b>{rec.treaty}</b>{rec.type_label ? ` · ${rec.type_label}` : ""}
            </div>
            {/* 26.3 — every treaty is configured from its own signed wording. */}
            <div className="sub" style={{ marginTop: 4, fontSize: 12,
                 ...(rec.wording ? {} : { color: "var(--p-warn-ink)" }) }}>
              {rec.wording
                ? (rec.wording.source === "esigned"
                    ? "Signed in Kavachio — the sealed copy"
                    : `Signed copy: ${rec.wording.filename ?? "attached"}`)
                : "Not supplied — the treaty cannot be configured until its signed wording is attached or it is signed."}
            </div>
            <Link className="linkish" to={`/contracts/${cid}`} style={{ fontSize: 12 }}>
              {rec.wording ? "Open the treaty and its wording" : "Attach the signed wording on the treaty"}
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
