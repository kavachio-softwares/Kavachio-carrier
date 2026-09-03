/**
 * What a bordereau run produced — the whole output half of Process Bordereau.
 *
 * ONE implementation, used by the carrier's screen and the broker's. The two
 * differ in how they choose WHAT to run (a carrier picks carrier → programme →
 * broker → contract; a broker picks one of its own contracts) and in which
 * routes they may call — a broker seat carries no tenant, so it reads its runs
 * through the carrier-centric chain instead of /export/downloads/*. None of
 * that changes what a result LOOKS like, so none of it belongs in two places:
 * a fix to the highlight legend or the fix-list CSV should reach both screens
 * at once.
 *
 * The caller supplies `urls`, which is the only thing that actually differs:
 *   file(id)  where the generated output is downloaded from
 *   data(id)  where the preview rows come from, with the same query params
 *
 * Everything below reads the run payload, which is identical either way.
 */
import { useState } from "react";
import { api, downloadFile } from "../api/client";
import {
  InlineAllRows, HighlightGrid, firstDataSheet, HL_BG, HL_BD,
  HL_WARN_BG, HL_WARN_BD, type Sheet,
} from "./OutputRows";

export type RunException = {
  severity?: string; sheet?: string; row?: number;
  column?: string; field?: string; rule_name?: string;
  policy_number?: string; actual_value?: string | number;
  expected_value?: string | number; reason?: string; message?: string;
};

export type GoverningContract = {
  sheet: string; contract_id: number | null;
  contract_filename: string | null; fallback: boolean;
};

export type RunResp = {
  export_id: number; filename: string; row_count: number;
  exception_count: number; exceptions: RunException[];
  status: string; datamodel_mapped: boolean; admin_task_id?: number | null;
  datamodel_queued?: boolean;
  format_drift?: boolean;
  governing_contracts?: GoverningContract[];
  /** True when produced by the pre-submission self-check — not ingested, not
   *  recorded as a run. */
  check_only?: boolean;
};

export type RunUrls = {
  file: (exportId: number) => string;
  data: (exportId: number, query: string) => string;
};

/** Fetch the preview sheet for a just-finished run. Best-effort by design: a
 *  missing preview must never make a successful run look like a failure. */
export function fetchPreview(urls: RunUrls, exportId: number): Promise<Sheet | null> {
  return api.get<{ sheets: Sheet[] }>(urls.data(exportId, "marks=1"))
    .then(r => firstDataSheet(r.data.sheets ?? []))
    .catch(() => null);
}

export function RunResult({
  result, preview, urls, onError, actions, footNote,
}: {
  result: RunResp;
  preview: Sheet | null;
  urls: RunUrls;
  onError?: (msg: string) => void;
  /** Screen-specific buttons in the summary bar (Review Exceptions, etc.). */
  actions?: React.ReactNode;
  /** Screen-specific line under the notes (e.g. who to ask about a drift). */
  footNote?: React.ReactNode;
}) {
  const [showAll, setShowAll] = useState(false);
  const [allSheets, setAllSheets] = useState<Sheet[] | null>(null);
  const [allBusy, setAllBusy] = useState(false);

  const isCheck = !!result.check_only;
  const spine = result.status === "clean" ? "ok" : "warn";

  // "See all rows": the full output (every sheet, all rows) with the same
  // highlighting as the download, expanded IN PLACE rather than in a modal.
  // Fetched once (full=1 lifts the row cap, marks=1 returns the flagged cells)
  // and cached, so re-expanding is instant.
  async function openAllRows() {
    setShowAll(true);
    if (allSheets) return;
    setAllBusy(true);
    try {
      const { data } = await api.get<{ sheets: Sheet[] }>(
        urls.data(result.export_id, "full=1&marks=1"));
      setAllSheets(Array.isArray(data.sheets) ? data.sheets : []);
    } catch { setAllSheets([]); }
    finally { setAllBusy(false); }
  }

  // The findings as a one-row-per-exception CSV — a downloadable fix-list.
  // Self-contained (the exceptions already carry every field), so it does not
  // depend on the review page's grouped exporter.
  function downloadFixList() {
    const header = ["Severity", "Rule", "Policy", "Sheet", "Column",
                    "Actual value", "Expected", "Reason"];
    const cell = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const rows = result.exceptions.map(e => [
      e.severity, e.rule_name, e.policy_number, e.sheet,
      e.column ?? e.field, e.actual_value, e.expected_value,
      e.reason ?? e.message,
    ].map(cell).join(","));
    const csv = [header.map(cell).join(","), ...rows].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = `check_${result.filename.replace(/\.[^.]+$/, "")}.csv`;
    a.click(); URL.revokeObjectURL(url);
  }

  return (
    <>
      <div className={`card spine ${spine} pad`}
        style={{ margin: "18px 0", display: "flex", alignItems: "center", gap: 20, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 3 }}>
            {isCheck && result.status === "clean"
              ? <span style={{ color: "var(--p-ok)" }}>✓ Ready to send — no issues found</span>
              : <>
                  {result.row_count.toLocaleString()} rows {isCheck ? "checked" : "validated"}
                  {result.status === "clean"
                    ? <span style={{ color: "var(--p-ok)" }}> · Clean</span>
                    : <span style={{ color: "var(--p-crit)" }}> · {result.exception_count.toLocaleString()} {isCheck ? "to fix" : "exceptions"}</span>}
                </>}
          </div>
          <div style={{ color: "var(--p-muted)", fontSize: 13 }}>
            {isCheck
              ? "Self-check only — nothing was sent or saved. Fix any issues and check again, or Generate BDX to send."
              : "Output generated. Exceptions don't block the file — review, or fix and re-run."}
          </div>
        </div>
        {isCheck && result.exception_count > 0 && (
          <button className="btn" onClick={downloadFixList}>Download Fix-List (CSV)</button>
        )}
        <button className="btn"
          onClick={() => downloadFile(urls.file(result.export_id), result.filename)
            .catch(() => onError?.("We couldn't download that file — please try again."))}>
          {isCheck ? "Download Checked File" : "Download BDX"}
        </button>
        {actions}
      </div>

      {/* The fix-list, inline, so the findings can be corrected before sending.
          Read-only — a check is a look, not a submission. */}
      {isCheck && result.exceptions.length > 0 && (
        <div className="card" style={{ marginBottom: 18 }}>
          <div className="card-h">
            <h3>What to Fix Before Sending</h3>
            <span className="sub">
              {result.exception_count.toLocaleString()} finding{result.exception_count === 1 ? "" : "s"}
            </span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Severity</th><th>Policy</th><th>Field</th><th>Value</th><th>Why</th></tr>
              </thead>
              <tbody>
                {result.exceptions.slice(0, 200).map((e, i) => {
                  const sev = (e.severity || "").toLowerCase();
                  const tone = sev.includes("crit") || sev === "error" ? "var(--p-crit)"
                    : sev.includes("warn") ? "var(--p-warn, #b45309)" : "var(--p-muted)";
                  return (
                    <tr key={i}>
                      <td style={{ color: tone, fontWeight: 600, whiteSpace: "nowrap" }}>
                        {sev.includes("crit") || sev === "error" ? "Critical"
                          : sev.includes("warn") ? "Warning" : (e.severity || "Info")}
                      </td>
                      <td className="mono">{e.policy_number ?? (e.row != null ? `Row ${e.row}` : "—")}</td>
                      <td>
                        {e.column ?? e.field ?? "—"}
                        {e.sheet ? <span className="sub" style={{ marginLeft: 6 }}>{e.sheet}</span> : null}
                      </td>
                      <td className="mono">{e.actual_value != null ? String(e.actual_value) : "—"}</td>
                      <td style={{ color: "var(--p-muted)" }}>{e.reason ?? e.message ?? "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {result.exceptions.length > 200 && (
            <div className="note" style={{ margin: 12 }}>
              Showing the first 200 of {result.exception_count.toLocaleString()} — download the CSV for the full list.
            </div>
          )}
        </div>
      )}

      {/* Which contract validated each output sheet. A compact one-liner when
          they all agree; a table when schedules answer to different contracts. */}
      {result.governing_contracts && result.governing_contracts.length > 0 && (() => {
        const gcs = result.governing_contracts!;
        const withContract = gcs.filter(g => g.contract_id != null);
        const distinct = new Set(withContract.map(g => g.contract_id));
        const nameOf = (g: GoverningContract) =>
          g.contract_filename || `Contract #${g.contract_id}`;
        if (distinct.size === 1 && withContract.length === gcs.length) {
          return (
            <div className="note" style={{ marginBottom: 18 }}>
              All output sheets validated against <strong>{nameOf(gcs[0])}</strong>.
            </div>
          );
        }
        return (
          <div className="card" style={{ marginBottom: 18 }}>
            <div className="card-h">
              <h3>Governing Contracts</h3>
              <span className="sub">
                {distinct.size} contract{distinct.size === 1 ? "" : "s"} · {withContract.length}/{gcs.length} sheets covered
              </span>
            </div>
            <div className="tbl-wrap">
              <table>
                <thead><tr><th>Output Sheet</th><th>Enforced By</th></tr></thead>
                <tbody>
                  {gcs.map(g => (
                    <tr key={g.sheet}>
                      <td className="mono">{g.sheet}</td>
                      <td>
                        {g.contract_id != null
                          ? <>{nameOf(g)}{g.fallback && <span className="sub" style={{ marginLeft: 8 }}>(default)</span>}</>
                          : <span className="sub">No contract</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })()}

      {result.format_drift && (
        <div className="note warn" style={{ marginBottom: 18 }}>
          This file's columns differ from the setup's input template — the output
          may be incomplete. {footNote}
        </div>
      )}
      {!isCheck && !result.datamodel_mapped && (
        <div className="note" style={{ marginBottom: 18 }}>
          New input format — a one-time admin task was raised to map it to the data model
          {result.admin_task_id ? ` (task #${result.admin_task_id})` : ""}. Delivery is complete regardless.
        </div>
      )}

      {/* Output Preview — the first rows by default, expanding IN PLACE to the
          whole output rather than into a modal. Read-only: this is the file that
          was just generated. */}
      {preview && preview.rows.length > 1 && (
        showAll ? (
          <InlineAllRows
            title="Output Preview"
            subtitle={`${result.filename} — cells that failed validation are highlighted, the same as in the downloaded file. Hover a cell to see why.`}
            sheets={allSheets ?? []}
            busy={allBusy}
            actions={<button className="btn sm" onClick={() => setShowAll(false)}>Show Less</button>}
          />
        ) : (
          <div className="card">
            <div className="card-h">
              <h3>Output Preview</h3>
              <span className="sub" style={{ display: "flex", alignItems: "center", gap: 10 }}>
                {preview.sheet} · first {Math.min(5, preview.rows.length - 1)} of {preview.rows.length - 1} rows
                <button className="btn sm" onClick={openAllRows}>See All Rows</button>
              </span>
            </div>
            {(preview.marks?.length ?? 0) > 0 && (
              <div className="note" style={{ margin: "10px 12px 10px", display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
                <span style={{ width: 13, height: 13, borderRadius: 3, background: HL_BG, border: `1px solid ${HL_BD}`, display: "inline-block", flex: "0 0 auto" }} />
                <span style={{ width: 13, height: 13, borderRadius: 3, background: HL_WARN_BG, border: `1px solid ${HL_WARN_BD}`, display: "inline-block", flex: "0 0 auto", marginLeft: -4 }} />
                Cells that failed validation are highlighted — critical in red, warnings in orange, the same as in the downloaded file. Hover a cell to see why.
              </div>
            )}
            <div className="tbl-wrap">
              <HighlightGrid sheet={preview} limit={5} sticky />
            </div>
          </div>
        )
      )}
    </>
  );
}
