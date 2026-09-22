import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Boxes, FileSpreadsheet } from "lucide-react";
import { api } from "../api/client";
import { currentMga } from "../auth";
import { fmtStamp } from "../utils/date";
import { ListFilterBar } from "../components/ListFilterBar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

type SetupContract = { contract_id: number; sheet_key: string | null; filename: string | null };
type Setup = {
  id: number; name: string | null; broker_name: string | null;
  carrier_party_id: number | null; carrier_name: string | null;
  program_id: number | null; program_name: string | null;
  input_format_name: string | null; output_template_name: string | null;
  status: "draft" | "active" | "superseded";
  contracts: SetupContract[];
  modified_at: string | null;
};

// Fixed application states (a Pipeline.status column value, not tenant data) —
// the same closed set the builder page's own status pill already renders.
const STATUS_LABEL: Record<Setup["status"], string> = {
  active: "Active", draft: "Draft", superseded: "Superseded",
};
// The .proto badge each status wears — the same pills Parties and Contracts use.
const STATUS_BADGE: Record<Setup["status"], string> = {
  active: "b-ok", draft: "b-warn", superseded: "b-mut",
};

const PAGE_SIZE = 10;

export default function BordereauSetups() {
  const mga = currentMga();
  const nav = useNavigate();

  // Filter-dropdown options come back WITH the paged list (`with_facets=1`)
  // instead of a second, unpaginated fetch of every setup. That request pulled
  // the whole table purely to extract a handful of distinct names — a full
  // extra round-trip and payload per page load, which is expensive once the API
  // isn't on localhost. The server sends the distinct programmes for the whole
  // tenant (not just this page), so the dropdown still lists everything.
  type Facets = { programs: { id: number; name: string }[] };
  const [facets, setFacets] = useState<Facets | null>(null);

  const [q, setQ] = useState("");
  const dq = useDebouncedValue(q, 300);
  // No carrier filter or column: this list is one carrier's own setups, so
  // every row named the same carrier and the filter had one option.
  // ?program_id= opens the list filtered to one programme (the Programmes
  // stepper links here once a programme's setups are all in place).
  const [params] = useSearchParams();
  const [program, setProgram] = useState(params.get("program_id") ?? "");   // program_id, as a string
  const [status, setStatus] = useState("");

  const programOptions = facets?.programs ?? [];
  // A programme passed in the URL that has no setups is dropped rather than
  // silently filtering on a value its own dropdown does not list. Not before
  // the facets arrive: an empty list on first render would throw it away.
  useEffect(() => {
    if (!facets) return;
    if (program && !programOptions.some(p => String(p.id) === program)) setProgram("");
  }, [facets]);

  // TRUE server-side pagination: the backend filters (q/program/status)
  // + pages; we send the current filters and receive just this page + total.
  const filterKey = `${dq}|${program}|${status}`;
  const { page, setPage, items, total, loading, pageCount } = useServerList<Setup>(
    (page, pageSize) =>
      api.get<{ items: Setup[]; total: number; facets?: Facets }>("/pipelines", {
        params: {
          mga, page, page_size: pageSize,
          q: dq || undefined,
          program_id: program || undefined,
          status: status || undefined,
          with_facets: true,
        },
      }).then(r => { if (r.data.facets) setFacets(r.data.facets); return r.data; }),
    filterKey,
    PAGE_SIZE,
  );

  const filtersActive = q !== "" || program !== "" || status !== "";
  function clearFilters() { setQ(""); setProgram(""); setStatus(""); }

  const newSetup = (
    <button type="button" className="btn pri" onClick={() => nav("/direct/setup")}>
      <FileSpreadsheet size={15} /> Bordereau Setup
    </button>
  );

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Bordereau Setups</h2>
            <p>Every setup created across your programmes — open one to review its mapping.</p>
          </div>
          <div className="actions">{newSetup}</div>
        </div>

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Search setups…" }}
            selects={[
              {
                key: "programme", ariaLabel: "Filter by programme",
                value: program, onChange: setProgram,
                options: [{ value: "", label: "All programmes" },
                  ...programOptions.map(p => ({ value: String(p.id), label: p.name }))],
              },
              {
                key: "status", ariaLabel: "Filter by status",
                value: status, onChange: setStatus,
                options: [{ value: "", label: "All statuses" },
                  ...(Object.entries(STATUS_LABEL) as [Setup["status"], string][])
                    .map(([v, l]) => ({ value: v, label: l }))],
              },
            ]}
            onClear={clearFilters}
            active={filtersActive}
          />

          <div className="tbl-wrap">
            {loading && items.length === 0 ? (
              <div className="empty">Loading…</div>
            ) : total === 0 && !filtersActive ? (
              /* Genuinely no setups (vs "none match the filters" below). Derived
                 from an unfiltered empty result rather than a full row fetch. */
              <div className="empty">
                <Boxes size={26} style={{ margin: "0 auto 10px", display: "block" }} />
                No setups yet — build one to map a programme's bordereau.
                <div style={{ marginTop: 14 }}>{newSetup}</div>
              </div>
            ) : total === 0 ? (
              <div className="empty">No setups match your filters.</div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Programme</th>
                    <th>Broker</th>
                    <th>Status</th>
                    <th>Modified</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map(r => (
                    <tr key={r.id}>
                      <td><b>{r.program_name ?? "—"}</b></td>
                      <td>{r.broker_name ?? "—"}</td>
                      <td>
                        <span className={`badge ${STATUS_BADGE[r.status] ?? "b-mut"}`}>
                          <span className="d" />{STATUS_LABEL[r.status] ?? r.status}
                        </span>
                      </td>
                      <td className="mono">{fmtStamp(r.modified_at, "—")}</td>
                      <td>
                        <span className="linkish" role="button" tabIndex={0}
                          onClick={() => nav(`/direct/setups/${r.id}`)}
                          onKeyDown={e => { if (e.key === "Enter") nav(`/direct/setups/${r.id}`); }}>
                          View
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
            totalItems={total} onPageChange={setPage} noun="setups" />
        </div>
      </div>
    </div>
  );
}
