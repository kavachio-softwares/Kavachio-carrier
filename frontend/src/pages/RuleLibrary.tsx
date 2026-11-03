import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { isKavachioAdmin } from "../auth";
import { Rule, listRulesPaged, toggleRule, deleteRule } from "../api/ruleLibrary";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { Pagination } from "../components/Pagination";
import { ListFilterBar } from "../components/ListFilterBar";
import { InfoTip } from "../components/InfoTip";
import { Layers, RefreshCw, ShieldCheck } from "lucide-react";

const PAGE_SIZE = 10;

// How a rule takes effect, as three short steps — for the seats that can change
// rules. Shown as a plain card, not a warning: nothing is wrong, it is how it works.
const HOW_RULES_WORK: { icon: React.ElementType; text: React.ReactNode }[] = [
  { icon: ShieldCheck, text: <>Rules validate broker-submitted files.</> },
  { icon: RefreshCw, text: <>Rerun <b>Bordereau Setup</b> after any rule changes.</> },
  { icon: Layers, text: <>Existing programmes must be set up again to apply updates.</> },
];

// Severity → badge class, mirroring the exception screens' colour language.
function sevBadge(s: string): { cls: string; label: string } {
  const k = (s || "").toLowerCase();
  if (k === "critical") return { cls: "b-crit", label: "Critical" };
  if (k === "major") return { cls: "b-warn", label: "Major" };
  return { cls: "b-mut", label: "Minor" };
}

export default function RuleLibrary() {
  const nav = useNavigate();
  const platform = isKavachioAdmin();
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [delTarget, setDelTarget] = useState<Rule | null>(null);
  const [delBusy, setDelBusy] = useState(false);
  // The server's answer, not the seat hook's: a carrier user reads this list
  // but the four write endpoints refuse them. False until the first page lands
  // so nobody sees a button flash that they cannot use.
  const [canManage, setCanManage] = useState(false);

  // Filters — applied by the server before the page slice, so the pager counts
  // only what matches. The search is debounced: one request, not one per key.
  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [severity, setSeverity] = useState("");
  const [status, setStatus] = useState("");
  const dq = useDebouncedValue(q, 300);
  const filtersActive = q.trim() !== "" || !!type || !!severity || !!status;
  const clearFilters = () => { setQ(""); setType(""); setSeverity(""); setStatus(""); };
  const [types, setTypes] = useState<{ class_name: string; label: string | null }[]>([]);

  // One page, counted by the server. The edit form still reads the WHOLE list
  // through listRules(): there is no GET-one endpoint, so it finds its rule by
  // id out of the full set — which is why paging here had to be opt-in.
  const {
    items: rows, total, page, pageCount, loading, setPage, reload,
  } = useServerList<Rule>(
    (pg, size) => listRulesPaged(pg, size, {
      q: dq.trim(), severity, status, class_name: type,
    }).then(d => {
      setCanManage(d.can_manage !== false);
      if (d.types) setTypes(d.types);
      return d;
    }).catch(e => {
      setErr(e?.response?.data?.detail ?? "Couldn't load rules. Please try again.");
      throw e;
    }),
    [dq.trim(), type, severity, status].join("|"), PAGE_SIZE,
  );

  async function onToggle(r: Rule) {
    setBusyId(r.id); setMsg(null);
    try {
      await toggleRule(r.id, !r.is_active);
      // Re-read the page rather than patching the row in place: the list is the
      // server's now, and a locally edited copy would drift from it.
      reload();
      setMsg(`"${r.rule_name}" ${r.is_active ? "disabled" : "enabled"}.`);
    } catch (e: any) {
      setMsg(e?.response?.data?.detail ?? "Couldn't update the rule.");
    } finally { setBusyId(null); }
  }

  async function confirmDelete() {
    if (!delTarget) return;
    setDelBusy(true);
    try {
      await deleteRule(delTarget.id);
      reload();
      setMsg(`"${delTarget.rule_name}" was deleted.`);
      setDelTarget(null);
    } catch (e: any) {
      setMsg(e?.response?.data?.detail ?? "Couldn't delete the rule.");
    } finally { setDelBusy(false); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              {platform ? "Platform Generic Rules" : "Generic Rules"}{" "}
              <InfoTip text={platform
                ? "Generic checks that run on every broker's BDX files. Changes here affect all tenants."
                : canManage
                  ? "Your own generic rules. They run on all your BDX files, alongside Kavachio's "
                    + "standard checks, and only your team can see them."
                  : "The checks your company runs on every BDX file. The carrier looks after this list."} />
            </h2>
          </div>
          {canManage && (
            <div className="actions">
              <button className="btn pri" onClick={() => nav("/rule-library/new")}>＋ New Rule</button>
            </div>
          )}
        </div>

        {err && <div className="note warn" style={{ marginBottom: 14, maxWidth: 640 }}>{err}</div>}
        {msg && <div className="note ok" style={{ marginBottom: 14, maxWidth: 640 }}>{msg}</div>}

        {/* How generic rules take effect — they are bound to a program during
            Bordereau Setup, not retroactively. Shown for both roles. */}
        {canManage ? (
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-h" style={{ padding: "11px 18px" }}>
              <h3 style={{ fontSize: 13.5 }}>How rules work</h3>
            </div>
            <div style={{
              display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
              gap: 12, padding: "14px 18px",
            }}>
              {HOW_RULES_WORK.map(({ icon: Icon, text }, i) => (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 11 }}>
                  <span aria-hidden="true" style={{
                    width: 32, height: 32, borderRadius: 8, flex: "0 0 auto",
                    display: "grid", placeItems: "center",
                    background: "var(--p-primary-soft)", color: "var(--p-primary)",
                  }}><Icon size={16} strokeWidth={2} /></span>
                  <span style={{ fontSize: 13, color: "var(--p-ink)", lineHeight: 1.4 }}>{text}</span>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="note warn" style={{ marginBottom: 16 }}>
            <>
              <b>These generic rules run on all your BDX files.</b>
              <div style={{ marginTop: 6 }}>
                You can read these rules but not change them — only the carrier can add, edit,
                turn off or delete one. A rule starts working on a program when its <b>Bordereau Setup</b> is run.
              </div>
              <div style={{ marginTop: 6 }}>
                If a program is already set up, it won't use a new rule until you run its setup again.
              </div>
            </>
          </div>
        )}

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Search rules…" }}
            selects={[
              { key: "type", ariaLabel: "Filter by type", value: type, onChange: setType,
                options: [{ value: "", label: "All types" },
                  ...types.map(t => ({ value: t.class_name, label: t.label || t.class_name }))] },
              { key: "severity", ariaLabel: "Filter by severity", value: severity, onChange: setSeverity,
                options: [{ value: "", label: "All severities" },
                  { value: "critical", label: "Critical" }, { value: "major", label: "Major" },
                  { value: "minor", label: "Minor" }] },
              { key: "status", ariaLabel: "Filter by status", value: status, onChange: setStatus,
                options: [{ value: "", label: "All statuses" },
                  { value: "active", label: "Active" }, { value: "disabled", label: "Disabled" }] },
            ]}
            onClear={clearFilters} active={filtersActive} />
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Rule</th><th>Type</th><th>Severity</th><th>Status</th>{canManage && <th className="r">Action</th>}</tr>
              </thead>
              <tbody>
                {rows.map(r => {
                  const sb = sevBadge(r.severity);
                  return (
                    <tr key={r.id} style={r.is_active ? undefined : { opacity: 0.6 }}>
                      <td>
                        <b>{r.rule_name}</b>
                        {r.validation_logic && <div className="sub">{r.validation_logic}</div>}
                      </td>
                      <td className="muted">{r.class_label ?? r.class_name}</td>
                      <td><span className={`badge ${sb.cls}`}><span className="d" />{sb.label}</span></td>
                      <td>
                        <span className={`badge ${r.is_active ? "b-ok" : "b-mut"}`}>
                          <span className="d" />{r.is_active ? "Active" : "Disabled"}
                        </span>
                      </td>
                      {canManage && (
                      <td className="r">
                        <div style={{ display: "flex", gap: 6, justifyContent: "center", flexWrap: "nowrap" }}>
                          <button type="button" className="btn sm"
                            onClick={() => nav(`/rule-library/${r.id}/edit`)}>Edit</button>
                          <button type="button" className="btn sm" disabled={busyId === r.id}
                            onClick={() => onToggle(r)}>
                            {r.is_active ? "Disable" : "Enable"}
                          </button>
                          <button type="button" className="btn sm danger" title="Delete this rule"
                            onClick={() => setDelTarget(r)}>Delete</button>
                        </div>
                      </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!loading && rows.length === 0 && filtersActive && (
              <div className="empty">No rules match these filters.</div>
            )}
            {!loading && rows.length === 0 && !filtersActive && (
              <div className="empty">
                {canManage
                  ? platform
                    ? <>No rules yet. Add a platform-wide check to get started.</>
                    : <>No rules of your own yet. Kavachio's standard checks already run on all your
                      files — add a rule here for anything specific to your business.</>
                  : "No rules yet. The carrier hasn't added any."}
              </div>
            )}
            {loading && <div className="empty">Loading…</div>}
          </div>
          <Pagination
            page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
            totalItems={total} onPageChange={setPage} noun="rules" />
        </div>

        <div className="note" style={{ marginTop: 14}}>
          {canManage
            ? <>A disabled rule is kept, so you can turn it back on at any time. Adding, editing, disabling
              or deleting a rule takes effect on a programme the next time its Bordereau Setup is run.</>
            : <>Disabled rules are kept but do not run. A change the carrier makes takes effect the
              next time a program's Bordereau Setup is run.</>}
        </div>
      </div>

      {delTarget && (
        <div className="proto-modal-overlay" onClick={() => !delBusy && setDelTarget(null)}>
          <div className="proto-modal" onClick={e => e.stopPropagation()}>
            <div className="m-h">
              <h3>Delete Rule</h3>
              <button className="x" onClick={() => !delBusy && setDelTarget(null)} aria-label="Close">×</button>
            </div>
            <div className="m-b">
              Delete <b>{delTarget.rule_name}</b>? This can't be undone. Files already processed
              are unaffected. Programmes already set up keep this check until their{" "}
              <b>Bordereau Setup</b> is run again.
            </div>
            <div className="m-f">
              <button className="btn" onClick={() => setDelTarget(null)} disabled={delBusy}>Cancel</button>
              <button className="btn danger" onClick={confirmDelete} disabled={delBusy}>
                {delBusy ? "Deleting…" : "Delete Rule"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
