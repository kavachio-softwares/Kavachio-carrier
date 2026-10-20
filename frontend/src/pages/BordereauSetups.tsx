import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowRight, Boxes, FileSpreadsheet, ShieldCheck, Upload } from "lucide-react";
import { api } from "../api/client";
import { currentMga } from "../auth";
import { fmtStamp } from "../utils/date";
import { ListFilterBar } from "../components/ListFilterBar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { InfoTip } from "../components/InfoTip";
import { getHierarchy } from "../api/hierarchy";
import { readyForSetupUrl } from "../components/ProgrammeStepper";
import { SETUP_STATUS as RI_STATUS } from "./ReinsuranceSetups";
import { listSetups, type SetupListItem } from "../api/ceded";
import { Modal } from "../components/ui/Modal";

/** The two kinds of bordereau setup a carrier has, as the list names them. */
type SetupType = "broker" | "reinsurance";
const SETUP_TYPE: Record<SetupType, { label: string; cls: string; what: string }> = {
  broker: { label: "Broker bordereau", cls: "b-info",
            what: "The files your brokers send you — read, checked against the contract and written to your output template." },
  reinsurance: { label: "Reinsurance bordereau", cls: "b-ok",
                 what: "The statements you send your reinsurers — worked out per treaty from your brokers' bordereaux." },
};

type SetupContract = { contract_id: number; sheet_key: string | null; filename: string | null };
type Setup = {
  id: number; name: string | null; broker_name: string | null;
  carrier_party_id: number | null; carrier_name: string | null;
  program_id: number | null; program_name: string | null;
  input_format_name: string | null; output_template_name: string | null;
  status: "draft" | "pending_approval" | "active" | "superseded";
  contracts: SetupContract[];
  modified_at: string | null;
  /** Who sent it up for the carrier admin's approval, and when. Both null on a
   *  setup that never went up — one the carrier admin built for themselves, or
   *  one from before approvals existed. */
  submitted_by: string | null;
  submitted_at: string | null;
};

// Fixed application states (a Pipeline.status column value, not tenant data) —
// the same closed set the builder page's own status pill already renders.
const STATUS_LABEL: Record<Setup["status"], string> = {
  active: "Active", pending_approval: "Awaiting approval",
  draft: "Draft", superseded: "Superseded",
};
// The .proto badge each status wears — the same pills Parties and Contracts use.
// Awaiting approval is info, not a warning: nothing has gone wrong, somebody
// simply has not looked at it yet.
const STATUS_BADGE: Record<Setup["status"], string> = {
  active: "b-ok", pending_approval: "b-info",
  draft: "b-warn", superseded: "b-mut",
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
  const [params, setParams] = useSearchParams();
  // Which kind of setup to list — a filter, like the others. ?tab=reinsurance
  // (the dashboard, Reinsurance Bordereaux and a treaty's setup link here)
  // opens it on the reinsurance setups.
  const [setupType, setSetupType] = useState<"" | SetupType>(
    params.get("tab") === "reinsurance" ? "reinsurance"
      : params.get("tab") === "broker" ? "broker" : "");
  void setParams;
  // The treaties' setups — one per live treaty, few enough to filter here.
  const [riItems, setRiItems] = useState<SetupListItem[] | null>(null);
  useEffect(() => {
    listSetups().then(setRiItems).catch(() => setRiItems([]));
  }, [mga]);
  // "What are you setting up?" — asked before a new setup is started.
  const [choosing, setChoosing] = useState(false);
  const [treatyPick, setTreatyPick] = useState("");
  const [program, setProgram] = useState(params.get("program_id") ?? "");   // program_id, as a string
  // ?status= opens the list filtered to one state — how the carrier admin's
  // "waiting on you" tile and the approval email both land on exactly the
  // setups they are about, rather than on the whole list to be found in.
  const [status, setStatus] = useState(params.get("status") ?? "");

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

  const filtersActive = q !== "" || program !== "" || status !== "" || setupType !== "";
  function clearFilters() { setQ(""); setProgram(""); setStatus(""); setSetupType(""); }

  // Which halves of the list show. A treaty's setup has no single programme,
  // so a programme filter shows broker setups only.
  const showBroker = setupType !== "reinsurance";
  const showRi = setupType !== "broker" && program === "";
  // Only setups that exist — a treaty nobody has started a setup for is not
  // a setup. It is started from "Bordereau Setup" → Reinsurance bordereau.
  const riRows = (riItems ?? []).filter(i => i.status != null
    && (!status || i.status === status)
    && (!dq.trim() || `${i.treaty} ${i.type_label ?? ""}`.toLowerCase()
      .includes(dq.trim().toLowerCase())));
  // Treaty setups sit at the top of the first page; the pages are the broker
  // setups', which can run to hundreds.
  const riShown = showRi && page === 1 ? riRows : [];
  const brokerRows = showBroker ? items : [];
  const shownTotal = (showBroker ? total : 0) + (showRi ? riRows.length : 0);

  // Does this carrier have ANY setup? Only learnt from an unfiltered answer —
  // a filtered empty page says nothing about the rest. Null until known, so
  // the button never flashes disabled while the first page loads.
  const [hasAny, setHasAny] = useState<boolean | null>(null);
  useEffect(() => {
    if (!loading && !filtersActive) setHasAny(total > 0);
  }, [loading, filtersActive, total]);

  // A programme ready for its setup — a broker on it with a settled contract
  // and no finished setup. Read from the same /hierarchy payload the
  // Programmes list works its steps out from, so the two screens agree.
  const [readyUrl, setReadyUrl] = useState<string | null>(null);
  useEffect(() => {
    getHierarchy().then(h => setReadyUrl(readyForSetupUrl(h.programmes)))
      .catch(() => setReadyUrl(null));
  }, [mga]);

  // Shut only while there is nothing to build a setup FROM: no setup yet and
  // no programme with a broker and a settled contract. Once a programme is
  // ready it opens, and — while there is no setup yet — goes straight to that
  // programme and broker, the same place the Programmes list's button goes.
  const noSetups = hasAny === false;
  const shut = noSetups && !readyUrl;
  const startBroker = () => nav(noSetups && readyUrl ? readyUrl : "/direct/setup");
  const newSetup = (
    <button type="button" className="btn pri" onClick={() => setChoosing(true)}>
      <FileSpreadsheet size={15} /> Bordereau Setup
    </button>
  );

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Bordereau Setups
              <InfoTip text={"Every bordereau setup: broker bordereaux (the files your "
                + "brokers send you) and reinsurance bordereaux (the statements you "
                + "send your reinsurers). Open one to review it."} />
            </h2>
          </div>
          <div className="actions">{newSetup}</div>
        </div>

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Search setups…" }}
            selects={[
              {
                key: "type", ariaLabel: "Filter by setup type",
                value: setupType, onChange: v => setSetupType(v as "" | SetupType),
                options: [{ value: "", label: "All setup types" },
                  { value: "broker", label: "Broker bordereau" },
                  { value: "reinsurance", label: "Reinsurance bordereau" }],
              },
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
            {(showBroker && loading && items.length === 0) || (showRi && riItems === null) ? (
              <div className="empty">Loading…</div>
            ) : shownTotal === 0 && !filtersActive ? (
              <div className="empty">
                <Boxes size={26} style={{ margin: "0 auto 10px", display: "block" }} />
                No setups yet.
                <div className="sub" style={{ marginTop: 6 }}>
                  Use <b>Bordereau Setup</b> at the top right and pick what you are
                  setting up.
                </div>
              </div>
            ) : shownTotal === 0 && status === "pending_approval"
                 && q === "" && program === "" ? (
              <div className="empty">
                <Boxes size={26} style={{ margin: "0 auto 10px", display: "block" }} />
                Nothing is waiting for your approval.
                <div className="sub" style={{ marginTop: 6 }}>
                  A setup appears here when a colleague finishes one and sends
                  it up.
                </div>
              </div>
            ) : shownTotal === 0 ? (
              <div className="empty">No setups match your filters.</div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Setup type</th>
                    <th>Programme / Treaty</th>
                    <th>Broker / Treaty type</th>
                    <th>Status</th>
                    <th>Modified</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {riShown.map(r => {
                    const st = RI_STATUS[r.status ?? "none"];
                    const to = `/reinsurance/setups/${r.contract_id}`;
                    return (
                      <tr key={`ri-${r.contract_id}`} style={{ cursor: "pointer" }}
                          onClick={() => nav(to)}>
                        <td><span className={`badge ${SETUP_TYPE.reinsurance.cls}`}>
                          <ShieldCheck size={11} /> {SETUP_TYPE.reinsurance.label}</span></td>
                        <td><b>{r.treaty}</b>
                          <div className="sub">{r.programmes} programme{r.programmes === 1 ? "" : "s"} feed it</div></td>
                        <td>{r.type_label ?? "—"}
                          {r.frequency && (
                            <div className="sub">
                              {{ monthly: "Monthly", quarterly: "Quarterly", annually: "Yearly" }[r.frequency]}
                              {r.send_mode ? ` · ${r.send_mode === "auto" ? "sent automatically" : "sent on Send"}` : ""}
                            </div>
                          )}</td>
                        <td><span className={`badge ${st.cls}`}><span className="d" />{st.label}</span></td>
                        <td className="mono">{fmtStamp(r.updated_at, "—")}</td>
                        <td>
                          <span className="linkish" role="button" tabIndex={0}
                            onClick={e => { e.stopPropagation(); nav(to); }}>
                            View
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                  {brokerRows.map(r => (
                    <tr key={r.id}>
                      <td><span className={`badge ${SETUP_TYPE.broker.cls}`}>
                        <Upload size={11} /> {SETUP_TYPE.broker.label}</span></td>
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

          {showBroker && total > PAGE_SIZE && (
            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={total} onPageChange={setPage} noun="broker setups" />
          )}
        </div>

        {/* What kind of setup — asked first, because the two are built from
            different things: a broker's file, or a treaty. */}
        <Modal open={choosing} size="3xl" title="What are you setting up?"
          onClose={() => { setChoosing(false); setTreatyPick(""); }}>
          <div className="proto" style={{ background: "transparent" }}>
            <div className="startpick" style={{ gridTemplateColumns: "repeat(2, minmax(0,1fr))" }}>
              <div className="sp" style={shut ? { opacity: .55, cursor: "not-allowed" } : undefined}
                   onClick={() => { if (!shut) startBroker(); }}
                   title={shut ? "Finish a programme first — it needs a broker and a signed contract (Programmes → Configure Program)." : undefined}>
                <b><Upload size={14} style={{ verticalAlign: "-2px" }} /> {SETUP_TYPE.broker.label}</b>
                <span>{SETUP_TYPE.broker.what}</span>
                <span style={{ marginTop: 8, fontWeight: 600, color: "var(--p-primary)" }}>
                  {shut ? "Finish a programme first"
                    : <>Pick the programme and broker <ArrowRight size={12} style={{ verticalAlign: "-2px" }} /></>}
                </span>
              </div>
              <div className="sp" style={{ cursor: "default" }}>
                <b><ShieldCheck size={14} style={{ verticalAlign: "-2px" }} /> {SETUP_TYPE.reinsurance.label}</b>
                <span>{SETUP_TYPE.reinsurance.what}</span>
                {(riItems ?? []).length === 0 ? (
                  <span style={{ marginTop: 8 }}>
                    No treaty yet.{" "}
                    <Link className="linkish" to="/contracts/new?type=insurer_reinsurer">Raise one</Link>
                  </span>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 10 }}
                       onClick={e => e.stopPropagation()}>
                    {/* .field gives it the form look every other dropdown has. */}
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Treaty</label>
                    <select value={treatyPick} onChange={e => setTreatyPick(e.target.value)}
                            style={{ width: "100%" }}>
                      <option value="">Choose the treaty…</option>
                      {(riItems ?? []).map(t => (
                        <option key={t.contract_id} value={String(t.contract_id)}>
                          {t.treaty}{t.status ? ` — ${RI_STATUS[t.status].label.toLowerCase()}` : " — not set up yet"}
                        </option>
                      ))}
                    </select>
                    </div>
                    <button type="button" className="btn pri" disabled={!treatyPick}
                            style={{ alignSelf: "flex-start" }}
                            onClick={() => nav(`/reinsurance/setups/${treatyPick}`)}>
                      Continue <ArrowRight size={13} />
                    </button>
                  </div>
                )}
              </div>
            </div>
          </div>
        </Modal>
      </div>
    </div>
  );
}
