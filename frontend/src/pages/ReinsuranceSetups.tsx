/**
 * Reinsurance setups — every treaty, and whether its reinsurance bordereau is
 * set up. Shown as the Reinsurance tab of Bordereau Setup: a treaty's
 * statements are not prepared or sent until its setup is active.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { Settings2 } from "lucide-react";
import { ListFilterBar } from "../components/ListFilterBar";
import { fieldErrors } from "../api/contractRecord";
import { listSetups, type SetupListItem } from "../api/ceded";
import { fmtDate } from "../utils/date";

export const SETUP_STATUS: Record<string, { label: string; cls: string }> = {
  active: { label: "Active", cls: "b-ok" },
  pending_approval: { label: "Awaiting approval", cls: "b-info" },
  draft: { label: "Draft", cls: "b-warn" },
  none: { label: "Not set up", cls: "b-mut" },
};

const FREQ: Record<string, string> = {
  monthly: "Monthly", quarterly: "Quarterly", annually: "Yearly",
};

/** The reinsurance tab of Bordereau Setup: every live treaty and whether its
 *  reinsurance bordereau is set up. */
export function ReinsuranceSetupsTable() {
  const nav = useNavigate();
  const [items, setItems] = useState<SetupListItem[] | null>(null);
  const [err, setErr] = useState("");

  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    listSetups().then(setItems).catch(e => setErr(fieldErrors(e).message));
  }, []);

  // Filtered in the page — a carrier has a handful of treaties, not pages of them.
  const shown = useMemo(() => (items ?? []).filter(i =>
    (!status || (i.status ?? "none") === status)
    && (!q.trim() || `${i.treaty} ${i.type_label ?? ""}`.toLowerCase()
      .includes(q.trim().toLowerCase()))), [items, q, status]);

  return (
    <>
        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Search treaties…" }}
            selects={[{
              key: "status", ariaLabel: "Filter by status", value: status, onChange: setStatus,
              options: [{ value: "", label: "All statuses" },
                ...Object.entries(SETUP_STATUS).map(([v, s]) => ({ value: v, label: s.label }))],
            }]}
            onClear={() => { setQ(""); setStatus(""); }}
            active={!!q || !!status} />
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Treaty</th><th>Status</th><th>Frequency</th><th>Sending</th>
                    <th>Programmes</th><th>Updated</th><th /></tr>
              </thead>
              <tbody>
                {shown.map(i => {
                  const st = SETUP_STATUS[i.status ?? "none"];
                  return (
                    <tr key={i.contract_id} style={{ cursor: "pointer" }}
                        onClick={() => nav(`/reinsurance/setups/${i.contract_id}`)}>
                      <td>
                        <b>{i.treaty}</b>
                        <div className="sub">{i.type_label ?? "No structure yet"}</div>
                      </td>
                      <td><span className={`badge ${st.cls}`}><span className="d" />{st.label}</span></td>
                      <td>{i.frequency ? FREQ[i.frequency] : "—"}</td>
                      <td>{i.send_mode ? (i.send_mode === "auto" ? "Automatic" : "On Send") : "—"}</td>
                      <td className="mono">{i.programmes}</td>
                      <td className="sub">{i.updated_at ? fmtDate(i.updated_at) : "—"}</td>
                      <td className="r">
                        <Link className={`btn sm ${i.status ? "" : "pri"}`}
                              to={`/reinsurance/setups/${i.contract_id}`}
                              onClick={e => e.stopPropagation()}>
                          <Settings2 size={12} /> {i.status ? "Open" : "Set up"}
                        </Link>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {items && items.length > 0 && shown.length === 0 && (
              <div className="empty">No treaty matches these filters.</div>
            )}
            {items?.length === 0 && (
              <div className="empty">
                No treaties yet.{" "}
                <Link className="linkish" to="/contracts/new?type=insurer_reinsurer">Raise one</Link>
              </div>
            )}
            {!items && !err && <div className="empty">Loading…</div>}
          </div>
        </div>
    </>
  );
}

/** The old address: the reinsurance setups now live in Bordereau Setup. */
export default function ReinsuranceSetups() {
  return <Navigate to="/direct/setups?tab=reinsurance" replace />;
}
