import { useEffect, useState } from "react";
import { Search } from "lucide-react";
import { getCarrierRanking, type BrokerInsightCarrier } from "../api/broker";
import { Pagination } from "../components/Pagination";

const PAGE_SIZE = 25;
const DAYS = 30;

/**
 * Every carrier this broker works with, ranked by files run — the rest of
 * what the dashboard's Files by Carrier card only has room for the top 5 of.
 */
export default function FilesByCarrier() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ items: BrokerInsightCarrier[]; total: number } | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => { setQuery(q.trim()); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    setErr(false);
    getCarrierRanking({ days: DAYS, page, pageSize: PAGE_SIZE, q: query })
      .then(setData).catch(() => setErr(true));
  }, [page, query]);

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Files by Carrier</h2>
            <p>Files run for each carrier in the last {DAYS} days.</p>
          </div>
        </div>

        <div className="card">
          <div style={{ padding: "16px 20px" }}>
            <label className="search">
              <Search className="ic" />
              <input type="search" placeholder="Search by carrier name"
                     aria-label="Search carriers"
                     value={q} onChange={e => setQ(e.target.value)} />
            </label>
          </div>

          {err ? (
            <div className="empty">Could not load your carriers.</div>
          ) : !data ? (
            <div className="empty">Loading…</div>
          ) : data.total === 0 ? (
            <div className="empty">{query ? `No carrier matches “${query}”.` : "No carriers yet."}</div>
          ) : (
            <>
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr><th>#</th><th>Carrier</th><th style={{ textAlign: "right" }}>Files</th></tr>
                  </thead>
                  <tbody>
                    {data.items.map(x => (
                      <tr key={x.id}>
                        <td className="muted">{x.rank}</td>
                        <td>{x.name}</td>
                        <td style={{ textAlign: "right", fontWeight: 600, fontVariantNumeric: "tabular-nums",
                                     color: x.runs ? undefined : "var(--p-faint)" }}>
                          {x.runs}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination page={page} pageSize={PAGE_SIZE} totalItems={data.total}
                          pageCount={Math.max(1, Math.ceil(data.total / PAGE_SIZE))}
                          onPageChange={setPage} noun="carriers" />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
