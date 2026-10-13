import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  getExportResults, type ExportResults, type RowOutcome,
} from "../api/validation";
import { downloadFile } from "../api/client";
import { InfoTip } from "./ui/InfoTip";
import { Pagination } from "./Pagination";

/** The Results tab of one checked file (plan 21–22): what happened to EVERY
 *  row, not only the ones that failed — Summary, All Records, and History &
 *  Downloads (each version with its own Excel and CSV). Read-only for
 *  everyone; deciding stays on the Exceptions tab. */

const OUTCOME: Record<RowOutcome, { label: string; badge: string }> = {
  passed:      { label: "Passed",        badge: "b-ok" },
  open:        { label: "Failed — open", badge: "b-crit" },
  resolved:    { label: "Resolved",      badge: "b-info" },
  not_checked: { label: "Not checked",   badge: "b-mut" },
};

const SOURCE: Record<string, string> = {
  upload: "Uploaded", email: "Email", sftp: "SFTP", api: "API", channel: "Channel",
  secure_link: "Secure Link", fix_validate: "Fix & Validate",
};

const PAGE = 50;

function when(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export default function ResultsPanel({ exportId, uploadId, fromParam }: {
  exportId: string; uploadId: string; fromParam: string | null;
}) {
  const navigate = useNavigate();
  const [res, setRes] = useState<ExportResults | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [outcome, setOutcome] = useState<RowOutcome | "">("");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

  // Typing settles for a moment before it asks the server.
  useEffect(() => {
    const t = setTimeout(() => { setQuery(q); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    let live = true;
    setBusy(true); setErr(null);
    getExportResults(exportId, { outcome, q: query, page, pageSize: PAGE })
      .then(r => { if (live) setRes(r); })
      .catch((e: any) => { if (live) setErr(e?.response?.data?.detail ?? e?.message ?? "Could not load the results."); })
      .finally(() => { if (live) setBusy(false); });
    return () => { live = false; };
  }, [exportId, outcome, query, page]);

  const s = res?.summary;
  const tile = (k: string, v: number | undefined, info: string, color?: string, o?: RowOutcome) => (
    <div className={`tile${o ? " click" : ""}`} style={{ cursor: o ? "pointer" : undefined,
      outline: o && outcome === o ? "2px solid var(--p-primary)" : undefined }}
      onClick={() => { if (o) { setOutcome(outcome === o ? "" : o); setPage(1); } }}>
      <div className="k" style={{ display: "flex", alignItems: "center", gap: 4 }}>{k} <InfoTip text={info} /></div>
      <div className="v" style={{ color }}>{v ?? "—"}</div>
    </div>
  );

  const openVersion = (eid: number | null) => {
    if (!eid) return;
    const qs = new URLSearchParams();
    qs.set("download", String(eid));
    qs.set("tab", "results");
    if (fromParam) qs.set("from", fromParam);
    navigate(`/uploads/${uploadId}/exceptions?${qs.toString()}`);
  };
  const save = (path: string) => downloadFile(path)
    .catch(() => alert("We couldn't download that file — please try again."));

  if (err && !res) return <div className="note warn">{err}</div>;

  return (
    <div>
      {/* ── Summary ── */}
      <div className="card pad" style={{ marginBottom: 18 }}>
        <div className="card-h" style={{ padding: 0, border: 0, marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Summary</h3>
          <span className="sub">Every row of this version of the file</span>
        </div>
        <div className="tiles five">
          {tile("Total rows", s?.total, "Every data row in this version of the file.")}
          {tile("Passed", s?.passed, "Checked, and no rule raised anything on the row.", "var(--p-ok)", "passed")}
          {tile("Failed / open", s?.open, "The row has at least one exception still waiting for a decision.", "var(--p-crit)", "open")}
          {tile("Resolved", s?.resolved, "The row had exceptions and every one has been approved, fixed, dismissed or rejected.", "var(--p-primary)", "resolved")}
          {tile("Not checked", s?.not_checked, "The rules never looked at the row: the file could not be validated, or the row is blank, a total line or a repeated header.", "var(--p-muted)", "not_checked")}
        </div>
        {s && (
          <p className="muted" style={{ fontSize: 12.5, margin: "10px 0 0" }}>
            {s.exceptions} exception{s.exceptions === 1 ? "" : "s"} in all · {s.exceptions_open} open · {s.exceptions_resolved} resolved
          </p>
        )}
      </div>

      {/* ── All Records ── */}
      <div className="card" style={{ marginBottom: 18 }}>
        <div className="card-h" style={{ flexWrap: "wrap", gap: 10 }}>
          <h3 style={{ margin: 0 }}>All Records</h3>
          <div className="seg" role="group" aria-label="Show rows">
            {(["", "passed", "open", "resolved", "not_checked"] as const).map(o => (
              <button key={o || "all"} className={outcome === o ? "on" : ""}
                onClick={() => { setOutcome(o); setPage(1); }}>
                {o ? OUTCOME[o].label : "All"}
              </button>
            ))}
          </div>
          <div className="search" style={{ minWidth: 240, marginLeft: "auto" }}>
            <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m20 20-3-3" /></svg>
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search policy, rule or column…" />
          </div>
        </div>
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th style={{ width: 70 }}>Row</th>
                <th>Policy</th>
                <th style={{ width: 150 }}>Result</th>
                <th className="l">Exceptions</th>
              </tr>
            </thead>
            <tbody>
              {(res?.records.items ?? []).map(r => (
                <tr key={`${r.sheet_name}-${r.row}`}>
                  <td className="mono">{r.sheet ? `${r.sheet} · ` : ""}{r.row}</td>
                  <td>{r.policy_number || <span className="muted">—</span>}</td>
                  <td><span className={`badge ${OUTCOME[r.outcome].badge}`}>{OUTCOME[r.outcome].label}</span></td>
                  <td className="l">
                    {r.exceptions.length === 0 ? <span className="muted">None</span> : (
                      <div style={{ display: "grid", gap: 3 }}>
                        {r.exceptions.map((x, i) => (
                          <div key={i} style={{ fontSize: 12.5 }}>
                            <b>{x.rule}</b>{x.field ? <span className="muted"> · {x.field}</span> : null}
                            {" — "}
                            <span style={{ color: x.status === "open" ? "var(--p-crit)" : "var(--p-ok)", fontWeight: 600 }}>
                              {x.status === "open" ? "open" : x.status}
                            </span>
                            {x.decided_by ? <span className="muted"> by {x.decided_by}</span> : null}
                          </div>
                        ))}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
              {!busy && (res?.records.items.length ?? 0) === 0 && (
                <tr><td colSpan={4} className="muted" style={{ textAlign: "center", padding: 18 }}>
                  No rows match.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
        {res && (
          <Pagination page={res.records.page} pageCount={res.records.pages}
            pageSize={res.records.page_size} totalItems={res.records.total}
            onPageChange={setPage} noun="rows" />
        )}
      </div>

      {/* ── History & Downloads ── */}
      <div className="card">
        <div className="card-h">
          <h3 style={{ margin: 0 }}>History &amp; Downloads</h3>
          <span className="sub">Each version keeps its own results and files</span>
        </div>
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th style={{ width: 110 }}>Version</th>
                <th>Received</th>
                <th>How</th>
                <th>Status</th>
                <th style={{ width: 110 }}>Exceptions</th>
                <th style={{ width: 290 }}></th>
              </tr>
            </thead>
            <tbody>
              {(res?.versions ?? []).map(v => (
                <tr key={`${v.version}-${v.export_id}`}>
                  <td style={{ whiteSpace: "nowrap" }}><b>Version {v.version}</b></td>
                  <td>{when(v.created_at)}</td>
                  <td>{v.source ? (SOURCE[v.source] ?? v.source) : "—"}</td>
                  <td>
                    {v.current
                      ? <span className="badge b-ok">Current</span>
                      : v.replaced_by
                        ? <span className="badge b-mut">Replaced by Version {v.replaced_by}</span>
                        : <span className="muted">{v.status_text ?? "—"}</span>}
                    {v.current && v.status_text ? <span className="muted" style={{ marginLeft: 6 }}>{v.status_text}</span> : null}
                  </td>
                  <td>{v.exceptions ?? "—"}</td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    {v.export_id ? (
                      <div style={{ display: "inline-flex", gap: 6 }}>
                        {String(v.export_id) !== String(exportId) && (
                          <button className="btn sm" onClick={() => openVersion(v.export_id)}>Open</button>
                        )}
                        <button className="btn sm" onClick={() => save(`/export/downloads/${v.export_id}/file`)}>
                          Excel
                        </button>
                        <button className="btn sm" onClick={() => save(`/export/downloads/${v.export_id}/results.csv`)}>
                          CSV
                        </button>
                      </div>
                    ) : <span className="muted">Not checked yet</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted" style={{ fontSize: 12, margin: "10px 16px 14px" }}>
          Excel is the file as it was made, with a Results sheet of counts. CSV has every row
          with its result and exceptions.
        </p>
      </div>
    </div>
  );
}
