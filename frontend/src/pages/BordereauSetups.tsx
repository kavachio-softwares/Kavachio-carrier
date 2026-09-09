import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Boxes, FileSpreadsheet, Search } from "lucide-react";
import { api } from "../api/client";
import { currentMga } from "../auth";
import { fmtStamp } from "../utils/date";
import { PageBody, PageHeader } from "../components/Layout";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Select } from "../components/ui/Field";
import { Sk } from "../components/ui/Skeleton";
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
// Matches the badge colors/dot the rest of the app's (.proto) tables use for
// status pills, e.g. Parties.tsx — same look, just reproduced with Tailwind
// arbitrary values since this page isn't wrapped in .proto.
function statusBadge(status: Setup["status"]) {
  if (status === "active") return { bg: "bg-[#E4F5EC]", text: "text-[#0A6E4C]", dot: "bg-[#0E9F6E]" };
  if (status === "superseded") return { bg: "bg-[#EEF0F5]", text: "text-[#566071]", dot: "bg-[#8B93A2]" };
  return { bg: "bg-[#FBF0DC]", text: "text-[#8F580D]", dot: "bg-[#C77A12]" }; // draft
}

const PAGE_SIZE = 10;

export default function BordereauSetups() {
  const mga = currentMga();
  const nav = useNavigate();

  // Filter-dropdown options come back WITH the paged list (`with_facets=1`)
  // instead of a second, unpaginated fetch of every setup. That request pulled
  // the whole table purely to extract a handful of distinct names — a full
  // extra round-trip and payload per page load, which is expensive once the API
  // isn't on localhost. The server sends the distinct carrier/program pairs for
  // the whole tenant (not just this page), so the dropdowns still list
  // everything.
  type Facets = {
    carriers: { id: number; name: string }[];
    programs: { id: number; name: string }[];
    pairs: { carrier_party_id: number; program_id: number }[];
  };
  const [facets, setFacets] = useState<Facets | null>(null);

  const [q, setQ] = useState("");
  const dq = useDebouncedValue(q, 300);
  const [carrier, setCarrier] = useState("");   // carrier_party_id, as a string
  const [program, setProgram] = useState("");   // program_id, as a string
  const [status, setStatus] = useState("");

  // Each side narrows to the OTHER side's current pick — picking a carrier
  // leaves only that carrier's programs selectable, and picking a program
  // leaves only its (one) carrier selectable — instead of always listing
  // every carrier/program regardless of what's already chosen.
  // `pairs` (carrier ↔ program combinations that actually have a setup) is what
  // lets each dropdown narrow to the other's pick, exactly as the full row list
  // used to — without shipping the rows.
  const carrierOptions = useMemo(() => {
    if (!facets) return [];
    if (!program) return facets.carriers;
    const ok = new Set(facets.pairs
      .filter(p => String(p.program_id) === program)
      .map(p => p.carrier_party_id));
    return facets.carriers.filter(c => ok.has(c.id));
  }, [facets, program]);
  const programOptions = useMemo(() => {
    if (!facets) return [];
    if (!carrier) return facets.programs;
    const ok = new Set(facets.pairs
      .filter(p => String(p.carrier_party_id) === carrier)
      .map(p => p.program_id));
    return facets.programs.filter(p => ok.has(p.id));
  }, [facets, carrier]);

  // If narrowing one side leaves the other's current selection no longer
  // valid (e.g. a program picked, then a carrier chosen that doesn't own it),
  // drop the now-invalid selection instead of silently filtering on a value
  // that no longer appears in its own dropdown.
  useEffect(() => {
    if (carrier && !carrierOptions.some(c => String(c.id) === carrier)) setCarrier("");
  }, [carrierOptions]);
  useEffect(() => {
    if (program && !programOptions.some(p => String(p.id) === program)) setProgram("");
  }, [programOptions]);

  // TRUE server-side pagination: the backend filters (q/carrier/program/status)
  // + pages; we send the current filters and receive just this page + total.
  const filterKey = `${dq}|${carrier}|${program}|${status}`;
  const { page, setPage, items, total, loading, pageCount } = useServerList<Setup>(
    (page, pageSize) =>
      api.get<{ items: Setup[]; total: number; facets?: Facets }>("/pipelines", {
        params: {
          mga, page, page_size: pageSize,
          q: dq || undefined,
          carrier_party_id: carrier || undefined,
          program_id: program || undefined,
          status: status || undefined,
          with_facets: true,
        },
      }).then(r => { if (r.data.facets) setFacets(r.data.facets); return r.data; }),
    filterKey,
    PAGE_SIZE,
  );
  const pageRows = items;
  const totalItems = total;

  const filtersActive = q !== "" || carrier !== "" || program !== "" || status !== "";
  function clearFilters() { setQ(""); setCarrier(""); setProgram(""); setStatus(""); }

  return (
    <>
      <PageHeader title="Bordereau Setups"
        subtitle="Every setup created across your carriers and programs — open one to review its mapping."
        action={<Button onClick={() => nav("/direct/setup")}>
          <FileSpreadsheet size={15} /> Bordereau Setup
        </Button>} />
      <PageBody>
        <Card>
          <div className="flex flex-wrap items-center gap-2.5 mb-4">
            <div className="input flex items-center gap-2 flex-1 min-w-[220px] max-w-sm">
              <Search size={14} className="text-ink-soft shrink-0" />
              <input className="flex-1 outline-none bg-transparent text-sm" placeholder="Search Setups…"
                value={q} onChange={e => setQ(e.target.value)} />
            </div>
            <Select className="!w-auto" aria-label="Filter by carrier" value={carrier}
              onChange={e => setCarrier(e.target.value)}>
              <option value="">All Carriers</option>
              {carrierOptions.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
            </Select>
            <Select className="!w-auto" aria-label="Filter by program" value={program}
              onChange={e => setProgram(e.target.value)}>
              <option value="">All Programs</option>
              {programOptions.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
            <Select className="!w-auto" aria-label="Filter by status" value={status}
              onChange={e => setStatus(e.target.value)}>
              <option value="">All Statuses</option>
              {(Object.entries(STATUS_LABEL) as [Setup["status"], string][])
                .map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </Select>
            {filtersActive && (
              <button className="text-xs text-navy hover:underline ml-auto" onClick={clearFilters}>
                Clear Filters
              </button>
            )}
          </div>

          {loading ? (
            <div className="space-y-2">
              {Array.from({ length: 5 }, (_, i) => <Sk key={i} className="h-11 w-full" />)}
            </div>
          ) : totalItems === 0 && !filtersActive ? (
            /* Genuinely no setups (vs "none match the filters" below). Derived
               from an unfiltered empty result rather than a full row fetch. */
            <div className="text-center py-14 text-sm text-ink-muted">
              <Boxes size={26} className="mx-auto mb-2.5 text-ink-soft" />
              No setups yet — build one to map a carrier + program's bordereau.
              <div className="mt-3">
                <Button onClick={() => nav("/direct/setup")}>
                  <FileSpreadsheet size={15} /> Bordereau Setup
                </Button>
              </div>
            </div>
          ) : totalItems === 0 ? (
            <div className="text-center py-14 text-sm text-ink-muted">No setups match your filters.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[13px]" style={{ borderCollapse: "collapse" }}>
                <thead>
                  <tr>
                    <th className="py-[11px] px-4 text-[10.5px] tracking-[.5px] uppercase text-[#8B93A2] font-bold border-b border-[#E5E8EE] bg-[#F7F8FB]">Carrier</th>
                    <th className="py-[11px] px-4 text-[10.5px] tracking-[.5px] uppercase text-[#8B93A2] font-bold border-b border-[#E5E8EE] bg-[#F7F8FB]">Program</th>
                    <th className="py-[11px] px-4 text-[10.5px] tracking-[.5px] uppercase text-[#8B93A2] font-bold border-b border-[#E5E8EE] bg-[#F7F8FB]">Broker</th>
                    <th className="py-[11px] px-4 text-[10.5px] tracking-[.5px] uppercase text-[#8B93A2] font-bold border-b border-[#E5E8EE] bg-[#F7F8FB]">Status</th>
                    <th className="py-[11px] px-4 text-[10.5px] tracking-[.5px] uppercase text-[#8B93A2] font-bold border-b border-[#E5E8EE] bg-[#F7F8FB]">Modified</th>
                    <th className="py-[11px] px-4 text-[10.5px] tracking-[.5px] uppercase text-[#8B93A2] font-bold border-b border-[#E5E8EE] bg-[#F7F8FB]">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {pageRows.map(r => {
                    const sb = statusBadge(r.status);
                    console.log('hello', r.carrier_name, r.program_name, r.status, r.broker_name);
                    return (
                    <tr key={r.id} className="group">
                      <td className="py-[13px] px-4 align-middle font-medium text-[#0E1320] border-b border-[#E5E8EE] group-last:border-b-0">{r.carrier_name ?? "—"}</td>
                      <td className="py-[13px] px-4 align-middle border-b border-[#E5E8EE] group-last:border-b-0">{r.program_name ?? "—"}</td>
                      <td className="py-[13px] px-4 align-middle border-b border-[#E5E8EE] group-last:border-b-0">{r.broker_name ?? "—"}</td>
                       <td className="py-[13px] px-4 align-middle border-b border-[#E5E8EE] group-last:border-b-0">
                        <span className={`inline-flex items-center gap-1.5 text-[11.5px] font-semibold px-2.5 py-[3px] rounded-full ${sb.bg} ${sb.text}`}>
                          <span className={`w-1.5 h-1.5 rounded-full ${sb.dot}`} />
                          {STATUS_LABEL[r.status] ?? r.status}
                        </span>
                      </td>
                      <td className="py-[13px] px-4 align-middle text-[#566071] border-b border-[#E5E8EE] group-last:border-b-0">{fmtStamp(r.modified_at, "—")}</td>
                      <td className="py-[13px] px-4 align-middle text-[#566071] border-b border-[#E5E8EE] group-last:border-b-0">
                        <button className="linkish" onClick={() => nav(`/direct/setups/${r.id}`)}>
                          View
                        </button>
                      </td>
                    </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {!loading && totalItems > 0 && (
            <div className="flex items-center justify-between mt-4 pt-3 border-t border-border text-xs text-ink-muted">
              <span>{(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, totalItems)} of {totalItems} Setups</span>
              <div className="flex items-center gap-2">
                <Button variant="secondary" className="!py-1 !px-2.5 !text-xs" disabled={page <= 1}
                  onClick={() => setPage(page - 1)}>← Prev</Button>
                <span>Page {page} of {pageCount}</span>
                <Button variant="secondary" className="!py-1 !px-2.5 !text-xs" disabled={page >= pageCount}
                  onClick={() => setPage(page + 1)}>Next →</Button>
              </div>
            </div>
          )}
        </Card>
      </PageBody>
    </>
  );
}
