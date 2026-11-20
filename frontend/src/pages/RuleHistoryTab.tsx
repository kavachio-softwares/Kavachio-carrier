// Audit Logs → Rule History (task 48.2, backend rule_history.py).
// The life of every check — a contract's own rules, Rule Library rules,
// Programme Rules, output template fields — and every mapping: created,
// changed, switched off or removed, version by version. The earlier version is
// kept on file rather than overwritten. Recording began when this shipped.
import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, Download, GitBranch, PencilLine, PowerOff, ShieldCheck } from "lucide-react";
import {
  downloadRuleHistory, getRuleHistory, getRuleVersions, type RuleFilters, type RulePage, type RuleRow,
  type RuleVersions,
} from "../api/changeHistory";
import { getHistoryChoices, type Choice } from "../api/auditHistory";
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
const STATUS_TONE: Record<string, string> = {
  Active: "b-ok", "In use": "b-ok", "Switched off": "b-crit", Removed: "b-crit", Retired: "b-mut",
  Draft: "b-info", "Not in use": "b-mut", "Kept on file": "b-mut", "Waiting for review": "b-warn",
};
const KIND_WORDS: Record<string, string> = {
  "Contract rule": "A contract's own check", "Rule Library": "Rule Library", "Programme rule": "Programme rule",
  "Output fields": "Output template fields", Mapping: "Mapping",
};

export default function RuleHistoryTab() {
  const range = useRange("6");
  const [f, setF] = useState<RuleFilters>({});
  const [typed, setTyped] = useState("");
  const q = useDebouncedValue(typed, 300);
  const [programmes, setProgrammes] = useState<Choice[]>([]);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => { getHistoryChoices().then(c => setProgrammes(c.programmes)).catch(() => {}); }, []);

  const filters: RuleFilters = useMemo(() => ({ ...f, ...range.dates, q }),
    [f, range.dates.from, range.dates.to, q]); // eslint-disable-line react-hooks/exhaustive-deps
  const list = useServerList<RuleRow, RulePage>(
    (p, n) => getRuleHistory(filters, p, n).then(r => { setFailed(false); return r; })
      .catch(e => { setFailed(true); throw e; }),
    JSON.stringify(filters), PAGE);
  const c = list.extra?.counts;
  const filtered = !!(f.kind || f.programme || f.status || q || range.changed);
  const set = (k: keyof RuleFilters) => (v: string) =>
    setF(x => ({ ...x, [k]: k === "programme" ? (v ? Number(v) : "") : v }));

  const save = async () => {
    setBusy(true);
    try { await downloadRuleHistory(filters); } catch (e) { showError(e, "Could not download the rule history"); }
    finally { setBusy(false); }
  };

  return (
    <>
      <Bar clear={filtered} onClear={() => { setF({}); setTyped(""); range.reset(); }}>
        <Pick label="Kind" value={f.kind ?? ""} onChange={set("kind")} all="All rules and mappings"
          options={(list.extra?.choices.kinds ?? Object.keys(KIND_WORDS)).map(k => ({ value: k, label: KIND_WORDS[k] ?? k }))} />
        <Pick label="Programme" value={f.programme ?? ""} onChange={set("programme")} all="All programmes"
          options={programmes.map(p => ({ value: p.id, label: p.name }))} />
        <Pick label="Status now" value={f.status ?? ""} onChange={set("status")} all="Any status"
          options={(list.extra?.choices.statuses ?? []).map(s => ({ value: s, label: s }))} />
        {range.field("Date")}
        <SearchBox value={typed} onChange={setTyped} placeholder="Search a rule, programme or contract" />
      </Bar>

      <div style={tiles(5)}>
        <Tile icon={ShieldCheck} label="Rules and mappings" value={c?.rules} colour="#7c3aed"
          info="Different rules and mappings with a recorded change in these dates." />
        <Tile icon={CheckCircle2} label="Created" value={c?.created} colour="#16a34a" info="Rules added." />
        <Tile icon={PencilLine} label="Changed" value={c?.changed} colour="#2563eb"
          info="Rules whose settings changed. The earlier version is kept on file." />
        <Tile icon={PowerOff} label="Switched off or removed" value={c?.off} colour="#dc2626"
          info="Rules switched off, retired, removed or deleted." />
        <Tile icon={GitBranch} label="Mappings changed" value={c?.mappings} colour="#d97706"
          info="Bordereau Setup and data mappings whose earlier version is kept on file." />
      </div>

      <div className="card">
        <div className="card-h">
          <h3>Every version of every rule
            <InfoTip text={"Newest first. Each line is one version: what happened and the version before it.\nClick a line to see every version with what changed, the earlier settings kept on file, and the rule's status now.\nRecorded from 19 Nov 2026."} /></h3>
          <button className="btn sm" style={{ marginLeft: "auto" }} disabled={busy || list.total === 0} onClick={save}>
            <Download size={13} /> {busy ? "Preparing…" : "Download CSV"}
          </button>
        </div>
        {listState(list.loading, list.items.length,
          filtered ? "No rule changes match these filters." : "No rule changes have been recorded in these dates.",
          failed) ?? (
          <>
            <div className="tbl-wrap"><table>
              <thead><tr><th style={L}>Rule</th><th style={L}>Programme / contract</th><th style={L}>Version</th>
                <th style={L}>What happened</th><th style={L}>Previous version</th>
                <th style={L}>By</th><th style={L}>Date</th></tr></thead>
              <tbody>{list.items.map(r => (
                <tr key={r.event_id} onClick={() => setOpen(r.key)} style={{ cursor: "pointer" }}
                  title="Click to see every version">
                  <td style={{ ...L, whiteSpace: "normal" }}><b>{r.name}</b>
                    <div className="sub">{KIND_WORDS[r.kind] ?? r.kind}</div></td>
                  <td style={{ ...L, whiteSpace: "normal" }}>{r.where}</td>
                  <td style={L}>V{r.version}</td>
                  <td style={L}><span className={`badge ${EVENT_TONE[r.event] ?? "b-mut"}`}>{r.event}</span>
                    {r.changes > 0 && <div className="sub">{r.changes} {r.changes === 1 ? "setting" : "settings"}</div>}</td>
                  <td style={L}>{r.previous ? `V${r.previous}` : <span className="muted">—</span>}</td>
                  <td style={L}>{r.by}{r.reason && <div className="sub" style={{ whiteSpace: "normal" }}>“{r.reason}”</div>}</td>
                  <td style={{ ...L, whiteSpace: "nowrap" }} className="muted">{fmtDateTime(r.at)}</td>
                </tr>))}</tbody>
            </table></div>
            <Pagination page={list.page} pageSize={PAGE} totalItems={list.total} pageCount={list.pageCount}
              onPageChange={list.setPage} noun="versions" />
          </>)}
      </div>

      <RulePanel ruleKey={open} onClose={() => setOpen(null)} />
    </>
  );
}

function RulePanel({ ruleKey, onClose }: { ruleKey: string | null; onClose: () => void }) {
  const [d, setD] = useState<RuleVersions | null>(null);
  useEffect(() => {
    setD(null);
    if (ruleKey) getRuleVersions(ruleKey).then(setD).catch(e => { showError(e, "Could not open this rule's history"); onClose(); });
  }, [ruleKey]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <SidePanel open={ruleKey != null} title={d?.name ?? "Rule details"} sub={d && (KIND_WORDS[d.kind] ?? d.kind)}
      onClose={onClose}>
      {!d ? <div className="muted">Loading…</div> : (
        <>
          <Facts rows={[["Kind", KIND_WORDS[d.kind] ?? d.kind], ["Programme / contract", d.where ?? (d.kind === "Rule Library" ? "Rule Library" : null)],
                        ["Version now", `V${d.version}`],
                        ["Status now", <span className={`badge ${STATUS_TONE[d.status] ?? "b-mut"}`}>{d.status}</span>]]} />
          <h4 style={{ margin: "4px 0 10px" }}>Versions</h4>
          {d.versions.map(v => (
            <div key={v.event_id} className="card" style={{ padding: "10px 12px", marginBottom: 10 }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 6 }}>
                <b>V{v.version}</b>
                <span className={`badge ${EVENT_TONE[v.event] ?? "b-mut"}`}>{v.event}</span>
                {v.previous && <span className="sub">from V{v.previous}</span>}
                <span className="sub" style={{ marginLeft: "auto" }}>{fmtDateTime(v.at)} · {v.by}</span>
              </div>
              {v.reason && <div style={{ marginBottom: 6 }}>Reason: {val(v.reason)}</div>}
              {v.cause && <div className="sub" style={{ marginBottom: 6 }}>{v.cause}.</div>}
              {v.changes.length > 0 && <ChangesTable changes={v.changes} more={v.more} />}
              <KeptOnFile before={v.before} />
            </div>))}
        </>)}
    </SidePanel>
  );
}
