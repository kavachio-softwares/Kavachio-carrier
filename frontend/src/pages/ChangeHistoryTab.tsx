// Audit Logs → Change History (task 48.1, backend change_history.py).
// Every field changed on an important record — programmes, companies, users,
// contracts, Bordereau Setups, mappings, output templates and rules — with its
// old and new value, who changed it, when, and the reason they gave.
// Recording began when this shipped; earlier edits are on Audit Trails only.
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Download, FilePenLine, Layers, MessageSquareText, Users } from "lucide-react";
import {
  downloadChanges, getChange, getChanges, type ChangeDetail, type ChangeFilters, type ChangePage, type ChangeRow,
} from "../api/changeHistory";
import { getAuditOptions, type AuditOptions } from "../api/audit";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { Pagination } from "../components/Pagination";
import { InfoTip } from "../components/InfoTip";
import { showError } from "../components/ResultPopup";
import { fmtDateTime } from "../utils/date";
import { Facts, SidePanel, Tile, tiles } from "./AuditHistoryTabs";
import { Bar, ChangesTable, KeptOnFile, L, listState, Pick, SearchBox, useRange, val } from "./AuditTabParts";

const PAGE = 20;
const EVENT_TONE: Record<string, string> = {
  Created: "b-ok", Changed: "b-info", Deleted: "b-crit", Removed: "b-crit", "Switched off": "b-warn",
  "Switched on": "b-ok", Retired: "b-mut",
};

export default function ChangeHistoryTab() {
  const range = useRange("3");
  const [module, setModule] = useState("");
  const [actor, setActor] = useState("");
  const [entity, setEntity] = useState<{ key: string; name: string } | null>(null);
  const [typed, setTyped] = useState("");
  const q = useDebouncedValue(typed, 300);
  const [opts, setOpts] = useState<AuditOptions | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  // Opened from elsewhere — Contract History's "Draft changed" links here
  // with ?change=<id> (task 49.3).
  const [search, setSearch] = useSearchParams();
  const linked = Number(search.get("change")) || null;
  const [open, setOpen] = useState<{ id: number; field?: string | null } | null>(linked ? { id: linked } : null);
  const close = () => {
    setOpen(null);
    if (search.get("change")) { const n = new URLSearchParams(search); n.delete("change"); setSearch(n, { replace: true }); }
  };
  useEffect(() => { getAuditOptions().then(setOpts).catch(() => setOpts(null)); }, []);

  const filters: ChangeFilters = useMemo(
    () => ({ ...range.dates, module, actor, q, entity: entity?.key }),
    [range.dates.from, range.dates.to, module, actor, q, entity], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const list = useServerList<ChangeRow, ChangePage>(
    (p, n) => getChanges(filters, p, n).then(r => { setFailed(false); return r; })
      .catch(e => { setFailed(true); throw e; }),
    JSON.stringify(filters), PAGE);
  const c = list.extra?.counts;
  const filtered = !!(module || actor || q || entity || range.changed);

  const save = async () => {
    setBusy(true);
    try { await downloadChanges(filters); } catch (e) { showError(e, "Could not download the change history"); }
    finally { setBusy(false); }
  };

  return (
    <>
      <Bar clear={filtered} onClear={() => { range.reset(); setModule(""); setActor(""); setTyped(""); setEntity(null); }}>
        {range.field("Date")}
        <Pick label="Module" value={module} onChange={setModule} all="All modules"
          options={(list.extra?.modules ?? []).map(m => ({ value: m, label: m }))} />
        <Pick label="Changed by" value={actor} onChange={setActor} all="Everyone"
          options={(opts?.actors ?? []).filter(a => a.key.startsWith("user:"))
            .map(a => ({ value: a.key, label: `${a.label} — ${a.role}` }))} />
        <SearchBox value={typed} onChange={setTyped} placeholder="Search a record, field or person" />
      </Bar>

      {entity && (
        <div className="note" style={{ marginBottom: 14, display: "flex", gap: 10, alignItems: "center" }}>
          Showing every change to <b>{entity.name}</b>.
          <span className="linkish" onClick={() => setEntity(null)}>Show all records</span>
        </div>)}

      <div style={tiles(4)}>
        <Tile icon={FilePenLine} label="Fields changed" value={c?.changes} colour="#7c3aed"
          info="Every field whose value changed in these dates, one per line below." />
        <Tile icon={Layers} label="Records changed" value={c?.records} colour="#2563eb"
          info="Different programmes, contracts, setups, rules and so on that were changed." />
        <Tile icon={Users} label="People" value={c?.people} colour="#0d9488"
          info="Different people who made the changes." />
        <Tile icon={MessageSquareText} label="Saves with a reason" value={c ? `${c.with_reason} of ${c.saves}` : null}
          colour="#d97706" info="Saves where the person said why. A reason is optional." />
      </div>

      <div className="card">
        <div className="card-h">
          <h3>Every change, field by field
            <InfoTip text={"Each line is one field: what it was, what it became, who changed it and why.\nClick a line to see the whole save and the record's earlier saves.\nRecorded from 19 Nov 2026; earlier edits show on Audit Trails without the values."} /></h3>
          <button className="btn sm" style={{ marginLeft: "auto" }} disabled={busy || list.total === 0} onClick={save}>
            <Download size={13} /> {busy ? "Preparing…" : "Download CSV"}
          </button>
        </div>
        {listState(list.loading, list.items.length,
          filtered ? "No changes match these filters." : "No changes have been recorded in these dates.", failed) ?? (
          <>
            <div className="tbl-wrap"><table>
              <thead><tr><th style={L}>Date</th><th style={L}>Record</th><th style={L}>Field changed</th>
                <th style={L}>Old value</th><th style={L}>New value</th><th style={L}>Changed by</th>
                <th style={L}>Reason</th></tr></thead>
              <tbody>{list.items.map((r, i) => (
                <tr key={`${r.event_id}-${r.line ?? i}`} onClick={() => setOpen({ id: r.event_id, field: r.field })} style={{ cursor: "pointer" }}
                  title="Click to see the whole save">
                  <td style={{ ...L, whiteSpace: "nowrap" }} className="muted">{fmtDateTime(r.at)}</td>
                  <td style={{ ...L, whiteSpace: "normal" }}><b>{r.name}</b>
                    <div className="sub">{r.kind} · {r.module}</div></td>
                  <td style={{ ...L, whiteSpace: "normal" }}>
                    {r.field ?? <span className={`badge ${EVENT_TONE[r.event] ?? "b-mut"}`}>{r.event}</span>}</td>
                  <td style={{ ...L, whiteSpace: "normal", maxWidth: 220 }}>{r.field ? val(r.old) : ""}</td>
                  <td style={{ ...L, whiteSpace: "normal", maxWidth: 220 }}>{r.field ? val(r.new) : ""}</td>
                  <td style={L}>{r.by}</td>
                  <td style={{ ...L, whiteSpace: "normal", maxWidth: 200 }}>{val(r.reason)}</td>
                </tr>))}</tbody>
            </table></div>
            <Pagination page={list.page} pageSize={PAGE} totalItems={list.total} pageCount={list.pageCount}
              onPageChange={list.setPage} noun="changes" />
          </>)}
      </div>

      <ChangeDetailPanel id={open?.id ?? null} highlight={open?.field} onClose={close}
        onShowRecord={(key, name) => { close(); setEntity({ key, name }); }} />
    </>
  );
}

/** One save in full — every field before and after, the reason, anything kept
 *  on file, and the record's other saves. Also opened from Contract History's
 *  "Draft changed" (task 49.3). */
export function ChangeDetailPanel({ id, highlight, onClose, onShowRecord }: {
  id: number | null; highlight?: string | null; onClose: () => void;
  onShowRecord?: (key: string, name: string) => void;
}) {
  const [cur, setCur] = useState<number | null>(id);
  const [d, setD] = useState<ChangeDetail | null>(null);
  useEffect(() => { setCur(id); }, [id]);
  useEffect(() => {
    setD(null);
    if (cur != null) getChange(cur).then(setD).catch(e => { showError(e, "Could not open this change"); onClose(); });
  }, [cur]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <SidePanel open={id != null} title="Change details" sub={d && `${d.kind}: ${d.name}`} onClose={onClose}>
      {!d ? <div className="muted">Loading…</div> : (
        <>
          <Facts rows={[["Record", <><b>{d.name}</b> · {d.kind}</>], ["Module", d.module],
                        ["What happened", d.event], ["Changed by", d.by], ["When", fmtDateTime(d.at)],
                        ["Reason", d.reason]]} />
          <h4 style={{ margin: "4px 0 10px" }}>Before / after</h4>
          <ChangesTable changes={d.changes} more={d.more} highlight={cur === id ? highlight : null} />
          <KeptOnFile before={d.before} />
          {d.history.length > 1 && (
            <>
              <h4 style={{ margin: "8px 0 8px" }}>Every save of this record</h4>
              <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
                {d.history.map(h => (
                  <li key={h.event_id}>
                    <button type="button" className="linkish" disabled={h.event_id === cur}
                      style={{ fontWeight: h.event_id === cur ? 700 : 400 }} onClick={() => setCur(h.event_id)}>
                      {fmtDateTime(h.at)} · {h.event} · {h.by}</button>
                    {h.reason && <div className="sub">“{h.reason}”</div>}
                  </li>))}
              </ol>
            </>)}
          {onShowRecord && (
            <div style={{ marginTop: 14 }}>
              <button type="button" className="btn sm" onClick={() => onShowRecord(d.target, d.name)}>
                View full history →</button>
            </div>)}
        </>)}
    </SidePanel>
  );
}
