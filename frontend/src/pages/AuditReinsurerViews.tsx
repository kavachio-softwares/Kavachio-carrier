// Audit Logs → Downloads & Views → "Reinsurers on their treaty links" (task
// 49.1, backend audit_reinsurer_views.py). Reinsurers open a treaty through a
// private link with no login, so their views never reached the main list;
// until now they showed only on the treaty's own page. Carriers and Kavachio
// see this card; a broker never does (the server says `hidden`).
import { useState } from "react";
import { Link } from "react-router-dom";
import { getReinsurerViews, type ReinsurerView, type ReinsurerViewPage } from "../api/changeHistory";
import type { AuditQuery } from "../api/audit";
import { useServerList } from "../hooks/useServerList";
import { Pagination } from "../components/Pagination";
import { InfoTip } from "../components/InfoTip";
import { fmtDateTime } from "../utils/date";
import { L, listState } from "./AuditTabParts";

const PAGE = 20;

export default function AuditReinsurerViews({ query }: { query: AuditQuery }) {
  const [kind, setKind] = useState("");
  const [failed, setFailed] = useState(false);
  const list = useServerList<ReinsurerView, ReinsurerViewPage>(
    (p, n) => getReinsurerViews({ from: query.from, to: query.to, q: query.q }, kind, p, n)
      .then(r => { setFailed(false); return r; })
      .catch(e => { setFailed(true); throw e; }),
    JSON.stringify([query.from, query.to, query.q, kind]), PAGE);
  if (list.extra?.hidden) return null;
  const c = list.extra?.counts;
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-h">
        <h3>Reinsurers on their treaty links
          <InfoTip text={"Reinsurers open a treaty through the private link you send them — they have no login.\nEvery time one viewed the treaty or downloaded a document, with the dates and search above."} /></h3>
        <span className="seg right">{[["", `All${c ? ` (${c.viewed + c.downloaded})` : ""}`],
                                      ["view", `Viewed${c ? ` (${c.viewed})` : ""}`],
                                      ["download", `Downloaded${c ? ` (${c.downloaded})` : ""}`]].map(([k, l]) => (
          <button key={k} type="button" className={kind === k ? "on" : ""} onClick={() => setKind(k)}>{l}</button>))}</span>
      </div>
      {listState(list.loading, list.items.length, "No reinsurer opened a treaty link in these dates.", failed) ?? (
        <>
          <div className="tbl-wrap"><table>
            <thead><tr><th style={L}>Date and time</th><th style={L}>Reinsurer</th><th style={L}>What they did</th>
              <th style={L}>Treaty</th></tr></thead>
            <tbody>{list.items.map(r => (
              <tr key={r.id}>
                <td style={{ ...L, whiteSpace: "nowrap" }} className="muted">{fmtDateTime(r.at)}</td>
                <td style={L}><b>{r.person}</b><div className="sub">{r.company ?? r.email ?? "—"}</div></td>
                <td style={L}><span className={`badge ${r.status === "Downloaded" ? "b-warn" : "b-info"}`}>{r.status}</span>
                  <div className="sub" style={{ marginTop: 2 }}>{r.what}{r.document ? ` — ${r.document}` : ""}</div></td>
                <td style={{ ...L, whiteSpace: "normal" }}>
                  <Link className="linkish" to={`/contracts/${r.contract_id}`}>{r.treaty}</Link></td>
              </tr>))}</tbody>
          </table></div>
          <Pagination page={list.page} pageSize={PAGE} totalItems={list.total} pageCount={list.pageCount}
            onPageChange={list.setPage} noun="entries" />
        </>)}
    </div>
  );
}
