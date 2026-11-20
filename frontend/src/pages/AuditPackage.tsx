// The audit package popup (backend audit_package.py, 50.1). It takes the
// Audit Logs filters as they stand — dates, programme, broker, policy — and
// asks only for the format: one file with every check that ran, its contract
// wording and page, every result and decision.
import { useState } from "react";
import { downloadAuditPackage, type PackageAsk } from "../api/auditHistory";
import { downloadErrorText } from "../api/client";
import { InfoTip } from "../components/InfoTip";
import { Modal } from "../components/ui/Modal";

const field: React.CSSProperties = { display: "grid", gap: 4, fontSize: 13.5 };

/** The package in one sentence: "Every check, result, error and decision from
 *  the last 30 days, for all programmes and all brokers." */
function sentence(shown: [string, string][]): string {
  const v = Object.fromEntries(shown) as Record<string, string>;
  const low = (x: string) => (/^(All|Last|The last)\b/.test(x) ? x.charAt(0).toLowerCase() + x.slice(1) : x);
  const dates = /^(last|the last)/i.test(v.Dates ?? "") ? `the ${low(v.Dates).replace(/^the /, "")}`
    : (v.Dates ?? "").replace(" – ", " to ");
  const policy = v.Policy ? `, policy ${v.Policy}` : "";
  return `Every check, result, error and decision from ${dates}, for ${low(v.Programme)} and ${low(v.Broker)}${policy}.`;
}

export function PackageModal({ ask, shown, notUsed, onClose }: {
  /** What the filters ask for, in the package's own terms. */
  ask: Omit<PackageAsk, "format">;
  /** The filters it uses, as they read on screen. */
  shown: [string, string][];
  /** Filters in use that a package cannot apply (Area, Action, Status, Search…). */
  notUsed: string[];
  onClose: () => void;
}) {
  const [format, setFormat] = useState<"excel" | "pdf">("excel");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function go() {
    setBusy(true); setError("");
    try {
      await downloadAuditPackage({ ...ask, format });
      onClose();
    } catch (e) {
      setError(await downloadErrorText(e, "Could not make the audit package."));
    } finally { setBusy(false); }
  }
  const seg = (on: boolean) => ({ className: on ? "on" : "" });
  return (
    <Modal open size="md" title="Download an audit package" onClose={() => !busy && onClose()}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
        <button className="btn pri" onClick={go} disabled={busy}>{busy ? "Preparing… this can take a minute" : "Download"}</button></div>}>
      <div style={{ display: "grid", gap: 14 }}>
        <div className="sub">
          {sentence(shown)}
          <InfoTip text={"One file with every check that ran, the contract wording and page it came from, every result, every error found and every decision made.\nSheets: Summary, Checks that ran, Results, Errors found, Decisions and a Column Guide.\nThe PDF lists up to 1,500 rows per section; the Excel file has every row." + (notUsed.length ? `\nNot used for the package: ${notUsed.join(", ")}.` : "")} />
        </div>
        <div style={field}>Format
          <span className="seg"><button type="button" {...seg(format === "excel")} onClick={() => setFormat("excel")}>Excel</button>
            <button type="button" {...seg(format === "pdf")} onClick={() => setFormat("pdf")}>PDF</button></span></div>
        {error && <div className="note crit">{error}</div>}
      </div>
    </Modal>
  );
}
