import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Search, ChevronDown, Check, X } from "lucide-react";
import { api, downloadFile } from "../api/client";
import { currentMga } from "../auth";
import { fmtStamp, localDayStart, localDayEnd } from "../utils/date";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

type Party = { id: number; legal_name: string };
type Run = {
  landing_id: number;
  source_filename: string | null;
  row_count: number;
  created_at: string | null;
  carrier_party_id: number | null;
  program_id: number | null;
  carrier_name: string | null;
  program_name: string | null;
  export_id: number;
  filename: string | null;
  exception_count: number;
  status: string;
};

const RESULT_FILTERS = [
  { v: "", label: "All Results" },
  { v: "clean", label: "Clean" },
  { v: "exceptions", label: "Needs Review" },
] as const;

// The endpoint returns up to 100 runs in one shot (no server-side paging), so
// filtering and pagination both happen client-side after fetch.
const PAGE_SIZE = 10;

export default function RecentRuns() {
  const mga = currentMga();
  const nav = useNavigate();
  const [searchParams] = useSearchParams();
  // Carries the sidebar-highlight context through to exception triage: whichever
  // screen linked into Run History (Dashboard or Process Bordereau) stays
  // highlighted while the user continues on from here.
  const fromParam = searchParams.get("from") === "direct" ? "direct" : "home";

  const [carriers, setCarriers] = useState<Party[]>([]);
  const [err, setErr] = useState<string | null>(null);

  // filters — carrierIds supports selecting several carriers at once. Seeded
  // from ?carrier=<id> when we arrive from Process Bordereau (comma-separated
  // ids are honoured too).
  const [q, setQ] = useState("");
  const dq = useDebouncedValue(q, 300);
  const [carrierIds, setCarrierIds] = useState<number[]>(() =>
    (searchParams.get("carrier") ?? "")
      .split(",").map(s => Number(s.trim())).filter(n => Number.isFinite(n) && n > 0));
  const [result, setResult] = useState<string>("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  useEffect(() => {
    api.get(`/parties`, { params: { mga, party_type: "carrier" } })
      .then(r => setCarriers(Array.isArray(r.data) ? r.data : (r.data?.items ?? [])))
      .catch(() => setCarriers([]));
  }, [mga]);

  const hasExc = (r: Run) => r.status !== "clean" && r.exception_count > 0;

  const iso = (d: Date | null) => (d ? d.toISOString() : "");

  // TRUE server-side pagination: the backend filters (q/carrier/result/date) +
  // pages. The old endpoint capped at 100 runs total with no way to reach
  // anything older — this fixes that; any number of runs is now reachable.
  const filterKey = `${dq}|${carrierIds.join(",")}|${result}|${dateFrom}|${dateTo}`;
  const { page, setPage, items, total, loading, pageCount } = useServerList<Run>(
    (page, pageSize) =>
      api.get<{ items: Run[]; total: number }>("/direct/runs", {
        params: {
          mga, page, page_size: pageSize,
          q: dq || undefined,
          carrier_ids: carrierIds.length ? carrierIds.join(",") : undefined,
          result: result || undefined,
          date_from: iso(localDayStart(dateFrom)) || undefined,
          date_to: iso(localDayEnd(dateTo)) || undefined,
        },
      }).then(r => r.data),
    filterKey,
    PAGE_SIZE,
  );
  const pageRows = items;
  const totalItems = total;

  const filtersActive = q.trim() !== "" || carrierIds.length > 0 || result !== "" || dateFrom !== "" || dateTo !== "";
  const navigate = useNavigate();
  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Process Bordereau History</h2>
            <p>Every bordereau you've processed — search or filter to find a run, then review its exceptions or download the output.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => navigate("/direct")}>
              ← Process Bordereau
            </button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}

        {/* Filter toolbar is its OWN card with overflow visible — the table
            card below keeps overflow:hidden for its rounded corners, which
            would otherwise clip the carrier dropdown. */}
        <div className="card" style={{ overflow: "visible", marginBottom: 14, padding: "14px 20px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
            <div className="search">
              <Search className="ic" />
              <input placeholder="Search by file, carrier or program…" value={q}
                onChange={e => setQ(e.target.value)} />
            </div>
            <CarrierMultiSelect carriers={carriers} selected={carrierIds} onChange={setCarrierIds} />
            <div className="seg sm">
              {RESULT_FILTERS.map(f => (
                <button key={f.v} className={result === f.v ? "on" : ""}
                  onClick={() => setResult(f.v)}>{f.label}</button>
              ))}
            </div>
            <div className="fbar-daterange">
              <input type="date" className="fbar-date" aria-label="Processed from date"
                value={dateFrom} onChange={e => setDateFrom(e.target.value)} />
              <span className="sub">–</span>
              <input type="date" className="fbar-date" aria-label="Processed to date"
                value={dateTo} onChange={e => setDateTo(e.target.value)} />
            </div>
            {filtersActive && (
              <span className="linkish" style={{ marginLeft: "auto" }}
                onClick={() => { setQ(""); setCarrierIds([]); setResult(""); setDateFrom(""); setDateTo(""); }}>
                Clear Filters
              </span>
            )}
          </div>
        </div>

        <div className="card">
          {loading ? null : totalItems === 0 ? (
            <div className="empty">
              {filtersActive
                ? "No runs match your search — try different filters."
                : "No runs yet — process a bordereau and it will appear here."}
            </div>
          ) : (
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Input File</th><th>Carrier</th><th>Program</th>
                    <th className="r">Policies</th><th>Result</th><th>Processed</th><th></th>
                  </tr>
                </thead>
                <tbody>
                  {pageRows.map(r => (
                    <tr key={r.landing_id}>
                      <td><b>{r.source_filename ?? r.filename ?? `Run #${r.landing_id}`}</b></td>
                      <td className="muted">{r.carrier_name ?? "—"}</td>
                      <td className="muted">{r.program_name ?? "—"}</td>
                      <td className="r">{(r.row_count ?? 0).toLocaleString()}</td>
                      <td>
                        <span className={`badge ${hasExc(r) ? "b-crit" : "b-ok"}`}>
                          <span className="d" />
                          {hasExc(r) ? `${r.exception_count.toLocaleString()} exceptions` : "Clean"}
                        </span>
                      </td>
                      <td className="muted">{fmtStamp(r.created_at, "")}</td>
                      <td className="r">
                        <span className="linkish"
                          onClick={() => hasExc(r)
                            ? nav(`/uploads/${r.export_id}/exceptions?download=${r.export_id}&from=${fromParam}`)
                            : downloadFile(`/export/downloads/${r.export_id}/file`, r.filename ?? undefined)
                                .catch(() => setErr("We couldn't download that file — please try again."))}>
                          {hasExc(r) ? "Review →" : "Download →"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {!loading && totalItems > 0 && (
            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={totalItems} onPageChange={setPage} noun="runs" />
          )}
        </div>
      </div>
    </div>
  );
}

// Styled multi-select for carriers: a pill-style trigger that opens a
// searchable checkbox list. Pick one or several carriers to filter the runs.
function CarrierMultiSelect({
  carriers, selected, onChange,
}: {
  carriers: Party[];
  selected: number[];
  onChange: (ids: number[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  // Close when clicking outside.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const selectedSet = new Set(selected);
  const toggle = (id: number) =>
    onChange(selectedSet.has(id) ? selected.filter(x => x !== id) : [...selected, id]);

  const needle = q.trim().toLowerCase();
  const visible = needle
    ? carriers.filter(c => c.legal_name.toLowerCase().includes(needle))
    : carriers;

  const label =
    selected.length === 0 ? "All Carriers"
    : selected.length === 1 ? (carriers.find(c => c.id === selected[0])?.legal_name ?? "1 Carrier")
    : `${selected.length} Carriers`;

  return (
    <div className="ms" ref={ref}>
      <button type="button" className={`ms-trigger${selected.length ? " active" : ""}`}
        onClick={() => setOpen(o => !o)}>
        <span className="ms-label">{label}</span>
        {selected.length > 0 && (
          <span className="ms-clear" role="button" aria-label="Clear carriers"
            onClick={e => { e.stopPropagation(); onChange([]); }}>
            <X size={13} />
          </span>
        )}
        <ChevronDown size={15} className="ms-caret" />
      </button>

      {open && (
        <div className="ms-pop">
          <div className="ms-search">
            <Search size={14} />
            <input autoFocus placeholder="Search carriers…" value={q}
              onChange={e => setQ(e.target.value)} />
          </div>
          <div className="ms-list">
            {visible.length === 0 ? (
              <div className="ms-empty">No carriers match.</div>
            ) : visible.map(c => {
              const on = selectedSet.has(c.id);
              return (
                <label key={c.id} className={`ms-opt${on ? " on" : ""}`}>
                  <input type="checkbox" checked={on} onChange={() => toggle(c.id)} />
                  <span className="ms-box">{on && <Check size={12} strokeWidth={3} />}</span>
                  <span className="ms-name">{c.legal_name}</span>
                </label>
              );
            })}
          </div>
          {selected.length > 0 && (
            <div className="ms-foot">
              <span className="linkish" onClick={() => onChange([])}>Clear Selection</span>
              <span className="muted">{selected.length} Selected</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
