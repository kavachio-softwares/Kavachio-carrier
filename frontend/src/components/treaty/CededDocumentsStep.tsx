/**
 * The Documents step of a reinsurance bordereau setup — the broker setup's
 * Documents tab, for a treaty: the files it is built from, as the same
 * upload boxes.
 *
 *   Input Template   a sample of the carrier's own input file. Optional:
 *                    left empty, the statement is read from the brokers'
 *                    bordereaux on the programmes the treaty protects.
 *   Output Template  the layout the reinsurers want. Optional: left empty,
 *                    Kavachio's columns are used (or the Lloyd's risk sheet,
 *                    built on the Output step).
 *   Treaty           the contract it is set up for — already on file.
 *
 * Each upload is read straight away (columns matched by the same AI pass a
 * broker's sample gets) and shows on the Input / Output / Field Mapping
 * steps. Nothing is saved until the setup is.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { FileSpreadsheet, FileUp, Loader2, ShieldCheck } from "lucide-react";
import { FileDrop } from "../ui/FileDrop";
import { fieldErrors } from "../../api/contractRecord";
import {
  uploadInputSample, uploadLayout, type SetupRecord, type SetupValues,
} from "../../api/ceded";

/** A file already read — shown in the box by its name. */
const named = (name?: string | null) => (name ? new File([], name) : null);

export default function CededDocumentsStep({ cid, rec, v, change }: {
  cid: number; rec: SetupRecord; v: SetupValues;
  change: (patch: Partial<SetupValues>) => void;
}) {
  const [busy, setBusy] = useState<"" | "input" | "output">("");
  const [err, setErr] = useState("");
  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  const outSrc = v.layout?.source ?? "kavachio";

  async function pickInput(f: File | null) {
    setErr("");
    if (!f) { change({ input_layout: { source: "brokers" } }); return; }
    setBusy("input");
    try {
      change({ input_layout: await uploadInputSample(cid, f) });
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  async function pickOutput(f: File | null) {
    setErr("");
    if (!f) {
      change({ layout: { source: "kavachio" }, columns: rec.defaults.columns,
               layout_file: null, file_format: "xlsx" });
      return;
    }
    setBusy("output");
    try {
      const p = await uploadLayout(cid, f);
      change({ layout: p.layout, columns: p.columns, layout_file: p.layout_file,
               file_format: p.file_format });
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <p className="sub" style={{ marginTop: 0, marginBottom: 12 }}>
        The files this treaty&rsquo;s reinsurance bordereau is built from. Both
        templates are optional — leave the input empty to read your brokers&rsquo;
        bordereaux, and the output empty to use Kavachio&rsquo;s columns.
      </p>
      {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}

      {/* The upload boxes are the broker setup's, which are styled for the
          app shell rather than .proto — so they sit outside it. */}
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4"
           style={{ fontFamily: "inherit" }}>
        <div>
          <FileDrop label="Input Template" icon={<FileUp size={15} />} tone="optional"
            file={busy === "input" ? null : named(own?.filename)}
            onPick={pickInput} accept=".xlsx,.xlsm,.csv"
            hint="Your own input file — or leave empty to read your brokers' bordereaux"
            disabled={busy === "input"} disabledNote="Reading your columns…" />
          <div className="sub" style={{ marginTop: 6 }}>
            {busy === "input" ? <><Loader2 size={11} className="animate-spin" /> Reading…</>
              : own ? `${new Set(own.columns.map(c => c.name)).size} columns — read each period from your file`
              : "Reads your brokers' bordereaux"}
          </div>
        </div>
        <div>
          <FileDrop label="Output Template" icon={<FileSpreadsheet size={15} />} tone="optional"
            file={busy === "output" ? null
              : outSrc === "uploaded" ? named(v.layout?.filename) : null}
            onPick={pickOutput} accept=".xlsx,.xlsm,.csv"
            hint="The layout your reinsurers want — or leave empty for Kavachio's columns"
            disabled={busy === "output"} disabledNote="Reading the layout…" />
          <div className="sub" style={{ marginTop: 6 }}>
            {busy === "output" ? <><Loader2 size={11} className="animate-spin" /> Reading…</>
              : outSrc === "uploaded" ? `${v.columns.length} columns, filled in as they are`
              : outSrc === "standard" ? "Built from the Lloyd's risk sheet"
              : "Kavachio's columns — or build from the Lloyd's risk sheet on the Output step"}
          </div>
        </div>
        <div className="sm:col-span-2 xl:col-span-1">
          <div style={{ border: "1.5px solid var(--p-border-2)", borderRadius: 10,
                        padding: "16px 14px", textAlign: "center", height: "100%" }}>
            <div style={{ display: "flex", gap: 6, justifyContent: "center", alignItems: "center",
                          fontWeight: 600, fontSize: 13 }}>
              <ShieldCheck size={15} /> Treaty
              <span className="badge b-ok" style={{ fontSize: 9.5 }}>On file</span>
            </div>
            <div style={{ marginTop: 6, fontSize: 12.5 }}>
              <b>{rec.treaty}</b>{rec.type_label ? ` · ${rec.type_label}` : ""}
            </div>
            <Link className="linkish" to={`/contracts/${cid}`} style={{ fontSize: 12 }}>
              Open the treaty and its wording
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}
