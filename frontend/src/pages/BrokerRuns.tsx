import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getBrokerRunHistory, exceptionsLeft, type OperatorRun } from "../api/broker";
import { canAccessPath } from "../access";
import { Pagination } from "../components/Pagination";
import { InfoTip } from "../components/InfoTip";
import { fmtDateTime, periodLabel } from "../utils/date";

const PAGE_SIZE = 20;

/** The exception screen a run opens on — the same one Process Bordereau uses. */
const reviewPath = (r: OperatorRun) =>
  `/uploads/${r.export_id}/exceptions?download=${r.export_id}&from=broker`;

/** Every run made for this broker — by its own team or by the carrier. */
export default function BrokerRuns() {
  const nav = useNavigate();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ items: OperatorRun[]; total: number } | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    getBrokerRunHistory(page, PAGE_SIZE).then(setData).catch(() => setErr(true));
  }, [page]);

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Processed Files
              <InfoTip text="Every bordereau processed for your broker, newest first — once per month, as its latest version. Open one to see its exceptions." />
            </h2>
          </div>
          {canAccessPath("/broker/bordereau") && (
            <div className="actions">
              <Link className="btn pri" to="/broker/bordereau">＋ Process Bordereau</Link>
            </div>
          )}
        </div>

        <div className="card">
          {err ? (
            <div className="empty">Could not load your runs.</div>
          ) : !data ? (
            <div className="empty">Loading…</div>
          ) : data.total === 0 ? (
            <div className="empty">No runs yet.</div>
          ) : (
            <>
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>File Name</th><th>Reporting Period</th><th>Programme</th><th>Contract</th><th>Sent by</th><th>Rows</th>
                      <th>Result</th><th>When</th><th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map(r => (
                      <tr key={r.export_id}>
                        <td>
                          <b>{r.filename}</b>
                          {r.version_no != null && r.version_no > 1 && (
                            <span className="vchip">Version {r.version_no}</span>)}
                        </td>
                        <td>{r.reporting_period ? periodLabel(r.reporting_period) : <span className="muted">—</span>}</td>
                        <td>{r.programme ?? "—"}</td>
                        <td>
                          {r.contract_id != null
                            ? <Link className="linkish" to={`/contracts/${r.contract_id}`}>{r.contract ?? `Contract #${r.contract_id}`}</Link>
                            : <span className="muted">{r.contract ?? "—"}</span>}
                        </td>
                        <td>{r.sent_by === "carrier" ? "The carrier" : "Your team"}</td>
                        <td>{r.rows ?? "—"}</td>
                        <td>
                          {r.exception_count > 0
                            ? (exceptionsLeft(r).open > 0
                              ? <span className="badge b-warn" title={exceptionsLeft(r).title}><span className="d" />
                                  {exceptionsLeft(r).open} of {r.exception_count} open</span>
                              : <span className="badge b-ok" title={exceptionsLeft(r).title}><span className="d" />
                                  All {r.exception_count} put right</span>)
                            : r.status === "not_validated"
                              ? <span className="badge"><span className="d" />Not Validated</span>
                              : <span className="badge b-ok"><span className="d" />Clean</span>}
                        </td>
                        <td className="muted">{fmtDateTime(r.created_at)}</td>
                        <td style={{ textAlign: "right" }}>
                          <button className="btn sm" onClick={() => nav(reviewPath(r))}>
                            {r.exception_count > 0 ? "Review →" : "Open →"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination page={page} pageSize={PAGE_SIZE} totalItems={data.total}
                          pageCount={Math.max(1, Math.ceil(data.total / PAGE_SIZE))}
                          onPageChange={setPage} noun="runs" />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
