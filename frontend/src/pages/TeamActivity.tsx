import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Search } from "lucide-react";
import { getTeamRanking, type BrokerInsightPerson } from "../api/broker";
import { Pagination } from "../components/Pagination";

const PAGE_SIZE = 25;
const DAYS = 30;

/**
 * The whole team's exception tally — the rest of what the dashboard's Team
 * Activity card only has room for the top 5 of. Ranking, searching and
 * paging all happen on the server, so this page costs the same to open
 * whether the team has ten people or a thousand.
 */
export default function TeamActivity() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ items: BrokerInsightPerson[]; total: number } | null>(null);
  const [err, setErr] = useState(false);

  // Wait for typing to pause before asking the server.
  useEffect(() => {
    const t = setTimeout(() => { setQuery(q.trim()); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    setErr(false);
    getTeamRanking({ days: DAYS, page, pageSize: PAGE_SIZE, q: query })
      .then(setData).catch(() => setErr(true));
  }, [page, query]);

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Team Activity</h2>
            <p>Exceptions each person put right in the last {DAYS} days.</p>
          </div>
        </div>

        <div className="card">
          <div style={{ padding: "16px 20px" }}>
            <label className="search">
              <Search className="ic" />
              <input type="search" placeholder="Search by name or email"
                     aria-label="Search your team"
                     value={q} onChange={e => setQ(e.target.value)} />
            </label>
          </div>

          {err ? (
            <div className="empty">Could not load your team.</div>
          ) : !data ? (
            <div className="empty">Loading…</div>
          ) : data.total === 0 ? (
            <div className="empty">{query ? `No one matches “${query}”.` : "No one on your team yet."}</div>
          ) : (
            <>
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr><th>#</th><th>Name</th><th style={{ textAlign: "right" }}>Put right</th></tr>
                  </thead>
                  <tbody>
                    {data.items.map(u => (
                      <tr key={u.id}>
                        <td className="muted">{u.rank}</td>
                        <td>
                          <Link to={`/broker/team-activity/${u.id}`}>{u.name}</Link>
                          {u.role === "broker_admin" && <span className="muted" style={{ fontSize: 12 }}> · admin</span>}
                        </td>
                        <td style={{ textAlign: "right" }}>
                          {u.resolved > 0 ? (
                            <Link className="btn sm" to={`/broker/team-activity/${u.id}`}>{u.resolved} →</Link>
                          ) : (
                            <span style={{ fontWeight: 600, fontVariantNumeric: "tabular-nums",
                                          color: "var(--p-faint)" }}>0</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination page={page} pageSize={PAGE_SIZE} totalItems={data.total}
                          pageCount={Math.max(1, Math.ceil(data.total / PAGE_SIZE))}
                          onPageChange={setPage} noun="people" />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
