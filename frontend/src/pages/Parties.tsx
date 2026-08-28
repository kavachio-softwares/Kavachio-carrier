import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Search } from "lucide-react";
import { api } from "../api/client";
import { currentMga, isTenantAdmin } from "../auth";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

type Party = {
  id: number; scope: string; party_type: string; legal_name: string;
  dba_name?: string; is_active: boolean;
};

// Friendly type labels.
const TYPE_LABEL: Record<string, string> = {
  // Legacy rows may still carry `insurer`/`reinsurer`; both read as Carrier.
  insurer: "Carrier", carrier: "Carrier", reinsurer: "Carrier",
};
// Segmented filter — value sent to the API, label shown.
const FILTERS = [
  { v: "", label: "All" },
  { v: "carrier", label: "Carrier" },
];
const PAGE_SIZE = 10;

export default function Parties() {
  const mga = currentMga();
  const nav = useNavigate();
  const isAdmin = isTenantAdmin();
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const dq = useDebouncedValue(q, 300);

  // TRUE server-side pagination: the backend filters (q/type/status) + pages;
  // we send the current filters and receive just this page + the matching
  // total. This directory renders the Active/Inactive badge and the Activate
  // link, so it opts out of the endpoint's active-only default (include_inactive)
  // unless a specific status is picked, in which case is_active narrows exactly.
  const filterKey = `${dq}|${type}|${status}`;
  const { page, setPage, items, total, pageCount, reload } = useServerList<Party>(
    (page, pageSize) =>
      api.get<{ items: Party[]; total: number }>("/parties", {
        params: {
          mga, page, page_size: pageSize,
          q: dq || undefined, party_type: type || undefined,
          include_inactive: status ? undefined : true,
          is_active: status ? status === "active" : undefined,
        },
      }).then(r => r.data),
    filterKey,
    PAGE_SIZE,
  );
  const totalItems = total;
  const pageRows = items;

  async function toggleActive(p: Party, e: React.MouseEvent) {
    e.stopPropagation();
    await api.put(`/parties/${p.id}`, {
      party_type: p.party_type, legal_name: p.legal_name, is_active: !p.is_active,
    });
    reload();
  }

  function clearFilters() { setQ(""); setType(""); setStatus(""); }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Carriers</h2>
            <p>Your directory of carriers.</p>
          </div>
          {isAdmin && (
            <div className="actions">
              <button className="btn pri" onClick={() => nav("/parties/new")}>＋ Add Carrier</button>
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-h" style={{ gap: 14, flexWrap: "wrap" }}>
            <div className="search">
              <Search className="ic" />
              <input placeholder="Search carriers…" value={q}
                onChange={e => setQ(e.target.value)} />
            </div>
            {/* <div className="seg sm">
              {FILTERS.map(f => (
                <button key={f.v} className={type === f.v ? "on" : ""}
                  onClick={() => setType(f.v)}>{f.label}</button>
              ))}
            </div> */}
            <select className="fbar-select" aria-label="Filter by status"
              value={status} onChange={e => setStatus(e.target.value)}>
              <option value="">All Statuses</option>
              <option value="active">Active</option>
              <option value="inactive">Inactive</option>
            </select>
            {(q !== "" || type !== "" || status !== "") && (
              <div className="right">
                <span className="linkish" onClick={clearFilters}>Clear Filters</span>
              </div>
            )}
          </div>

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Carrier</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th>Actions</th>
                  </tr>
              </thead>
              <tbody>
                {pageRows.map(p => {
                  const isCarrier = p.party_type === "carrier";
                  const isGlobal = p.scope === "global";
                  return (
                    <tr key={p.id}>
                      <td>
                        <b>{p.legal_name}</b>
                        {p.dba_name && <div className="sub">dba {p.dba_name}</div>}
                      </td>
                      <td>
                        <span className={`badge ${isCarrier ? "b-info" : "b-mut"}`}>
                          <span className="d" />{TYPE_LABEL[p.party_type] ?? p.party_type}
                        </span>
                      </td>
                      <td>
                        <span className={`badge ${p.is_active ? "b-ok" : "b-mut"}`}>
                          <span className="d" />{p.is_active ? "Active" : "Inactive"}
                        </span>
                      </td>
                      <td className="r">
                        <span className="linkish" onClick={() => nav(`/parties/${p.id}`)}>View</span>
                        {isGlobal ? (
                          <> · <span className="linkish mut">Kavachio</span></>
                        ) : isAdmin ? (
                          <> · <span className="linkish" onClick={e => toggleActive(p, e)}>
                            {p.is_active ? "Deactivate" : "Activate"}
                          </span></>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {totalItems === 0 && (
              <div className="empty">No parties match the filter.</div>
            )}
          </div>
          {totalItems > 0 && (
            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={totalItems} onPageChange={setPage} noun="carriers" />
          )}
        </div>
      </div>
    </div>
  );
}
