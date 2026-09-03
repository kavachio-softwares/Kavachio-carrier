/**
 * My Contracts — every contract this broker holds, across every programme a
 * carrier has put them on.
 *
 * Two kinds sit in one list. A contract the CARRIER added works straight away.
 * A contract the BROKER added waits until the carrier approves it, and only a
 * live one can be set up. That approval is the only permission a broker ever
 * needs — after it, the setup and everything downstream is theirs to do.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  getBrokerContracts, getBrokerCarriers,
  type BrokerContract, type BrokerCarrier,
} from "../api/broker";
import { fmtDate } from "../utils/date";
import { ListFilterBar } from "../components/ListFilterBar";

/** What the row means to the broker, which is not the raw column value. */
function approval(c: BrokerContract): { cls: string; label: string; note: string } {
  if (c.approval_status === "approved")
    return c.source === "carrier"
      ? { cls: "b-ok", label: "Live", note: "the carrier added it — no approval needed" }
      : { cls: "b-ok", label: "Approved", note: "the carrier approved it" };
  if (c.approval_status === "pending_approval")
    return { cls: "b-warn", label: "Pending carrier", note: "you cannot set it up yet" };
  if (c.approval_status === "rejected")
    return { cls: "b-crit", label: "Rejected", note: "sent back with a reason" };
  return { cls: "b-mut", label: "Draft", note: "not sent yet" };
}

export default function BrokerContracts() {
  const [rows, setRows] = useState<BrokerContract[] | null>(null);
  const [carriers, setCarriers] = useState<BrokerCarrier[]>([]);
  const [carrier, setCarrier] = useState("");
  const [status, setStatus] = useState("");
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => { getBrokerCarriers().then(setCarriers).catch(() => setCarriers([])); }, []);

  const load = useCallback(() => {
    getBrokerContracts({ carrierId: carrier ? Number(carrier) : undefined })
      .then(setRows)
      .catch(() => setErr("Could not load your contracts."));
  }, [carrier]);
  useEffect(load, [load]);

  const shown = (rows ?? []).filter(r => !status || r.approval_status === status);
  const filtersActive = carrier !== "" || status !== "";

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>My Contracts</h2>
            <p>
              Every contract you hold, across all the programmes you have been
              put on. You do not add the programme — the carrier does.
            </p>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 14, maxWidth: 560 }}>{err}</div>}

        <div className="note" style={{ marginBottom: 16 }}>
          <b>Two kinds of contract in one list.</b> Contracts the carrier adds
          for you work straight away. Contracts you add show as{" "}
          <b>Pending carrier</b> until they approve them, and only a live
          contract can be set up. That approval is the only permission you will
          ever need.
        </div>

        <div className="card">
          <ListFilterBar
            selects={[
              // Only the carriers that have actually put this broker on a
              // programme — a broker never picks a carrier freely.
              {
                key: "carrier", ariaLabel: "Filter by carrier", value: carrier, onChange: setCarrier,
                options: [{ value: "", label: "All carriers" },
                  ...carriers.map(c => ({ value: String(c.id), label: c.name }))],
              },
              {
                key: "status", ariaLabel: "Filter by approval", value: status, onChange: setStatus,
                options: [
                  { value: "", label: "All statuses" },
                  { value: "approved", label: "Live / Approved" },
                  { value: "pending_approval", label: "Pending carrier" },
                  { value: "rejected", label: "Rejected" },
                ],
              },
            ]}
            onClear={() => { setCarrier(""); setStatus(""); }}
            active={filtersActive}
          />

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Contract</th><th>Carrier</th><th>Programme</th>
                  <th>Term</th><th>Source</th><th>Approval</th><th>Bordereau</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(c => {
                  const a = approval(c);
                  // A live contract is one you can actually produce against,
                  // so the column offers the thing to DO rather than a status
                  // word — "Not set up yet" named a screen the broker has no
                  // access to and could do nothing about.
                  const canSetUp = c.approval_status === "approved";
                  return (
                    <tr key={c.id}>
                      <td>
                        <b>{c.filename ?? `Contract ${c.id}`}</b>
                        <div className="sub">{a.note}</div>
                      </td>
                      <td>{c.carrier.name}</td>
                      <td>{c.programme.name}</td>
                      <td className="muted">
                        {c.inception_dt && c.expiry_dt
                          ? `${c.inception_dt} → ${c.expiry_dt}` : "—"}
                      </td>
                      <td>{c.source === "carrier" ? "Carrier uploaded" : "You uploaded"}</td>
                      <td>
                        <span className={`badge ${a.cls}`}><span className="d" />{a.label}</span>
                      </td>
                      <td className={canSetUp ? "" : "muted"}>
                        {canSetUp
                          ? <Link to="/broker/bordereau">Process bordereau →</Link>
                          : "Locked"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {rows !== null && shown.length === 0 && (
              <div className="empty">
                {filtersActive
                  ? "No contracts match the filters."
                  : "No contracts yet. A carrier has to put you on a programme first."}
              </div>
            )}
            {rows === null && !err && <div className="empty">Loading…</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
