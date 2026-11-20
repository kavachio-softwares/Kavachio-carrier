// Audit Findings — the broker's side of Audit Management (backend
// audit_management.py): the findings carriers raised about this broker, from
// every carrier it works with, open ones first. The broker replies and adds
// evidence; only the carrier closes a finding. Never another broker's.
import { useEffect, useState } from "react";
import { brokerFile, brokerReply, getBrokerFindings, type Finding } from "../api/audits";
import { InfoTip } from "../components/InfoTip";
import { showError } from "../components/ResultPopup";
import { FindingCard } from "./AuditManagement";

export default function BrokerAuditFindings() {
  const [rows, setRows] = useState<Finding[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [show, setShow] = useState<"open" | "closed">("open");
  useEffect(() => { getBrokerFindings().then(d => setRows(d.findings)).catch(() => setFailed(true)); }, []);
  const open = (rows ?? []).filter(f => f.status !== "closed");
  const closed = (rows ?? []).filter(f => f.status === "closed");
  const list = show === "open" ? open : closed;
  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head"><div className="t">
          <h2>Audit Findings <InfoTip text={"Points a carrier found when it audited your company.\nReply and add evidence of what you did. The carrier closes a finding once it is fixed."} /></h2>
        </div></div>
        {failed ? <div className="card"><div className="empty">Could not load your audit findings.</div></div>
          : !rows ? <div className="card"><div className="empty">Loading…</div></div>
          : rows.length === 0 ? <div className="card"><div className="empty">No audit findings about your company.</div></div>
          : (<>
            <span className="seg" style={{ marginBottom: 14, display: "inline-flex" }}>
              <button type="button" className={show === "open" ? "on" : ""} onClick={() => setShow("open")}>Waiting for you ({open.length})</button>
              <button type="button" className={show === "closed" ? "on" : ""} onClick={() => setShow("closed")}>Closed ({closed.length})</button>
            </span>
            {list.length === 0 ? <div className="card"><div className="empty">{show === "open" ? "Nothing is waiting for you." : "No closed findings yet."}</div></div>
              : list.map(f => (
                <FindingCard key={f.id} f={f} side="broker"
                  onReply={async (note, file) => { const d = await brokerReply(f.id, note, file); setRows(d.findings); }}
                  onFile={(eid, name) => { brokerFile(f.id, eid, name).catch(e => showError(e, "Could not download the file")); }} />))}
          </>)}
      </div>
    </div>
  );
}
