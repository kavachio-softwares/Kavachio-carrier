import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { getPersonDecisions, type PersonDecision } from "../api/broker";
import { Pagination } from "../components/Pagination";
import { fmtDateTime } from "../utils/date";

const PAGE_SIZE = 25;
const DAYS = 30;

const KIND_LABEL: Record<string, string> = {
  fix: "Fixed", approve: "Approved", dismiss: "Dismissed", reject: "Rejected",
};

/** Where a decision → what changed, in words rather than raw column names. */
function describe(d: PersonDecision) {
  const where = [d.sheet, d.row != null ? `row ${d.row}` : null, d.field]
    .filter(Boolean).join(" · ");
  if (d.old_value != null && d.new_value != null && d.old_value !== d.new_value) {
    return `${where}: “${d.old_value}” → “${d.new_value}”`;
  }
  if (d.new_value != null) return `${where}: set to “${d.new_value}”`;
  return where || "—";
}

/**
 * WHICH exceptions this person put right — the receipt behind their number
 * on Team Activity. Every decision links back to the file it came from, so
 * "40 put right" stops being a total nobody can account for.
 */
export default function PersonActivity() {
  const { userId } = useParams();
  const nav = useNavigate();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Awaited<ReturnType<typeof getPersonDecisions>> | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    if (!userId) return;
    setErr(false);
    getPersonDecisions(Number(userId), { days: DAYS, page, pageSize: PAGE_SIZE })
      .then(setData).catch(() => setErr(true));
  }, [userId, page]);

  const reviewPath = (d: PersonDecision) =>
    `/uploads/${d.export_id}/exceptions?download=${d.export_id}&from=broker`;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>{data?.person.name ?? "Team member"}</h2>
            <p>What they put right in the last {DAYS} days.</p>
          </div>
          <div className="actions">
            <button className="btn" type="button" onClick={() => nav("/broker/team-activity")}>
              ← Back to Team Activity
            </button>
          </div>
        </div>

        <div className="card">
          {err ? (
            <div className="empty">Could not load this person's activity.</div>
          ) : !data ? (
            <div className="empty">Loading…</div>
          ) : data.total === 0 ? (
            <div className="empty">Nothing put right in this period.</div>
          ) : (
            <>
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>File</th><th>Policy</th><th>What changed</th>
                      <th>Kind</th><th>When</th><th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map(d => (
                      <tr key={d.id}>
                        <td>
                          <b>{d.filename ?? "—"}</b>
                          {d.programme && <div className="muted" style={{ fontSize: 12 }}>{d.programme}</div>}
                        </td>
                        <td>{d.policy_number ?? "—"}</td>
                        <td className="muted">{describe(d)}</td>
                        <td>{KIND_LABEL[d.kind] ?? d.kind}</td>
                        <td className="muted">{fmtDateTime(d.decided_at)}</td>
                        <td style={{ textAlign: "right" }}>
                          {d.export_id != null && (
                            <Link className="btn sm" to={reviewPath(d)}>Open file →</Link>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination page={page} pageSize={PAGE_SIZE} totalItems={data.total}
                          pageCount={Math.max(1, Math.ceil(data.total / PAGE_SIZE))}
                          onPageChange={setPage} noun="decisions" />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
