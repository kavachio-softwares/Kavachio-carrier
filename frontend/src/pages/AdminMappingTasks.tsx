import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { currentMga, isKavachioAdmin } from "../auth";
import { fmtDate, localDayStart, localDayEnd } from "../utils/date";
import { LoadingOverlay } from "../components/Busy";
import { ListFilterBar } from "../components/ListFilterBar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

type Task = {
  id: number; format_id: number | null; format_name: string | null;
  tenant_name: string | null;
  fingerprint: string | null; status: string; title: string | null;
  proposed_mapper_id: number | null;
  landing_record_ids: number[]; created_by: string | null;
  created_at: string | null;
};
type Broker = { id: number; name: string };
type TaskPage = {
  items: Task[]; total: number; open_count: number; resolved_count: number;
  brokers: Broker[];
};

const PAGE_SIZE = 10;
const iso = (d: Date | null) => (d ? d.toISOString() : "");

export default function AdminMappingTasks() {
  // The data-mapping queue is a PLATFORM-WIDE ops queue: it lists unknown input
  // formats from every broker, which is what the Platform Dashboard's "Open Map
  // Tasks" tile counts. A platform admin still belongs to some tenant, so
  // sending their own mga silently scoped the queue to that one tenant and the
  // other tenants' tasks vanished. Send no mga for a platform admin so the
  // backend's cross-tenant path runs; anyone else stays scoped as before.
  const mga = isKavachioAdmin() ? undefined : currentMga();
  const nav = useNavigate();
  const [busy, setBusy] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<"open" | "resolved">("open");

  const [q, setQ] = useState("");
  const [broker, setBroker] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const dq = useDebouncedValue(q, 300);

  // TRUE server-side pagination: the backend applies tab + search + date filters
  // and returns just this page, the matching total, and both tab counts.
  const filterKey = `${tab}|${dq}|${broker}|${dateFrom}|${dateTo}`;
  const { page, setPage, items, total, extra, loading, pageCount } =
    useServerList<Task, TaskPage>(
      (page, pageSize) =>
        api.get<TaskPage>("/admin/mapping-tasks", {
          params: {
            mga, tab, page, page_size: pageSize,
            q: dq || undefined,
            broker: broker || undefined,
            date_from: iso(localDayStart(dateFrom)) || undefined,
            date_to: iso(localDayEnd(dateTo)) || undefined,
          },
        }).then(r => r.data),
      filterKey,
      PAGE_SIZE,
    );

  const openCount = extra?.open_count ?? 0;
  const resolvedCount = extra?.resolved_count ?? 0;
  // Server-supplied and independent of the active filters, so the menu keeps
  // every broker even once one is selected.
  const brokers = extra?.brokers ?? [];

  function errText(e: unknown): string {
    const a = e as { response?: { data?: { detail?: string } }; message?: string };
    return a?.response?.data?.detail ?? a?.message ?? "Something went wrong";
  }

  // Build the AI mapping, then open the scored review UI (kept on this screen).
  async function buildMapping(t: Task) {
    if (busy) return;
    setBusy(t.id); setErr(null);
    try {
      const { data } = await api.post(`/admin/mapping-tasks/${t.id}/propose`);
      // Pass the task id so Approve & save on the review screen can close this
      // task + kick off the background backfill.
      nav(`/uploads/mapper/${data.mapper_id}?return=/admin/mapping-tasks&task=${t.id}`);
    } catch (e) { setErr(errText(e)); } finally { setBusy(null); }
  }
  // Open the already-proposed mapping in the review UI.
  function openTask(t: Task) {
    if (t.proposed_mapper_id) {
      nav(`/uploads/mapper/${t.proposed_mapper_id}?return=/admin/mapping-tasks&task=${t.id}`);
    } else {
      buildMapping(t);
    }
  }

  const filtersActive = q !== "" || broker !== "" || dateFrom !== "" || dateTo !== "";
  function clearFilters() { setQ(""); setBroker(""); setDateFrom(""); setDateTo(""); }

  function statusBadge(t: Task): { cls: string; label: string } {
    if (t.status === "done") return { cls: "b-ok", label: "Mapped" };
    if (t.status === "dismissed") return { cls: "b-mut", label: "Dismissed" };
    if (t.proposed_mapper_id) return { cls: "b-info", label: "Mapping Drafted" };
    return { cls: "b-warn", label: "Needs Mapping" };
  }

  return (
    <div className="proto">
      {busy != null && (
        <LoadingOverlay label="Proposing the kavachio mapping — this can take a minute…" />
      )}
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Data Mapping Queue</h2>
            <p>Unknown input formats waiting to be mapped into the canonical data model.</p>
          </div>
          <div className="actions">
            <div className="seg">
              <button className={tab === "open" ? "on" : ""} onClick={() => setTab("open")}>
                Open ({openCount})
              </button>
              <button className={tab === "resolved" ? "on" : ""} onClick={() => setTab("resolved")}>
                Resolved ({resolvedCount})
              </button>
            </div>
          </div>
        </div>

        {err && (
          <div className="note" style={{
            marginBottom: 14, maxWidth: 760,
            background: "var(--p-crit-soft)", borderColor: "#F3D2D7", color: "var(--p-crit-ink)"
          }}>
            {err}
          </div>
        )}
        <div className="card pad" style={{ marginBottom: 18, marginTop: 10 }}>
          <h3 style={{ margin: "0 0 18px", fontSize: 14 }}>How To Map</h3>
          <div className="steps">
            <div className="step s1">
              <div className="n">1</div>
              <div>
                <h4>Set Up Kavachio Mapping</h4>
                <p>
                  A new format shows as <b>Needs Mapping</b>. Click <b>Set Up Kavachio Mapping</b> to have the AI
                  propose a mapping from the source columns to the Kavachio canonical data model.
                </p>
              </div>
            </div>
            <div className="step s2">
              <div className="n">2</div>
              <div>
                <h4>Review &amp; Map The Columns</h4>
                <p>
                  A scored table shows each source column with its best-match canonical field and
                  confidence. Fix any low-confidence matches, then save. The task becomes
                  <b> Mapping Drafted</b> — use <b>Open</b> to re-open it any time.
                </p>
              </div>
            </div>
            <div className="step s3">
              <div className="n">3</div>
              <div>
                <h4>Approve &amp; Backfill</h4>
                <p>
                  Approving the mapping writes every pending file of this format into the data model,
                  and future files of the same format then map automatically.
                </p>
              </div>
            </div>
          </div>
        </div>

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Search formats…" }}
            selects={[
              {
                key: "broker", ariaLabel: "Filter by broker", value: broker, onChange: setBroker,
                options: [{ value: "", label: "All Brokers" },
                  ...brokers.map(b => ({ value: String(b.id), label: b.name }))],
              },
            ]}
            dateRange={{ from: dateFrom, onFromChange: setDateFrom, to: dateTo, onToChange: setDateTo }}
            onClear={clearFilters}
            active={filtersActive}
          />

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Source Format</th><th>Broker</th><th>Created On</th><th>Mapping Status</th><th>Action</th>
                </tr>
              </thead>
              <tbody>
                {items.map(t => {
                  const sb = statusBadge(t);
                  const needsMapping = !t.proposed_mapper_id;
                  const name = t.format_name || t.title || `Format #${t.format_id ?? "—"}`;
                  return (
                    <tr key={t.id}>
                      <td>
                        <b>{name}</b>
                      </td>
                      <td>{t.tenant_name ?? "—"}</td>
                      <td className="muted">{fmtDate(t.created_at, "—")}</td>
                      <td><span className={`badge ${sb.cls}`}><span className="d" />{sb.label}</span></td>
                      <td>
                        {tab !== "open" ? (
                          t.proposed_mapper_id
                            ? <span className="linkish" onClick={() => openTask(t)}>
                                {t.status === "done" ? "Mapped" : "Dismissed"} →
                              </span>
                            : <span className="linkish mut">{t.status === "done" ? "Mapped" : "Dismissed"}</span>
                        ) : needsMapping ? (
                          <button className="btn sm pri" disabled={busy != null} onClick={() => buildMapping(t)}>
                            ✨ Set Up Kavachio Mapping
                          </button>
                        ) : (
                          <span className="linkish" onClick={() => openTask(t)}>Open →</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!loading && total === 0 && (
              <div className="empty">
                {filtersActive
                  ? "No tasks match the filters."
                  : tab === "open"
                    ? "No pending tasks yet — all known formats are mapped."
                    : "No resolved tasks yet."}
              </div>
            )}
          </div>
          {!loading && total > 0 && (
            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={total} onPageChange={setPage} noun="tasks" />
          )}
        </div>


      </div>
    </div>
  );
}
