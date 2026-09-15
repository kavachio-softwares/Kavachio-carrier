import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { isKavachioAdmin } from "../auth";
import { Rule, listRulesPaged, toggleRule, deleteRule } from "../api/ruleLibrary";
import { useServerList } from "../hooks/useServerList";
import { Pagination } from "../components/Pagination";

const PAGE_SIZE = 10;

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

  // One page, counted by the server. The edit form still reads the WHOLE list
  // through listRules(): there is no GET-one endpoint, so it finds its rule by
  // id out of the full set — which is why paging here had to be opt-in.
  const {
    items: rows, total, page, pageCount, loading, setPage, reload,
  } = useServerList<Rule>(
    (pg, size) => listRulesPaged(pg, size).catch(e => {
      setErr(e?.response?.data?.detail ?? "Couldn't load rules. Please try again.");
      throw e;
    }),
    "", PAGE_SIZE,
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
            <h2>{platform ? "Platform Generic Rules" : "Generic Rules"}</h2>
            <p>
              {platform
                ? "Generic checks that run on every broker's BDX files. Changes here affect all tenants."
                : "Your generic rules. They run on all your BDX files and only your team can see them."}
            </p>
          </div>
          <div className="actions">
            <button className="btn pri" onClick={() => nav("/rule-library/new")}>＋ New Rule</button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 14, maxWidth: 640 }}>{err}</div>}
        {msg && <div className="note ok" style={{ marginBottom: 14, maxWidth: 640 }}>{msg}</div>}

        {/* How generic rules take effect — they are bound to a program during
            Bordereau Setup, not retroactively. Shown for both roles. */}
        <div className="note warn" style={{ marginBottom: 16 }}>
          <b>These generic rules run on {platform ? "every broker's" : "all your"} BDX files.</b>
          <div style={{ marginTop: 6 }}>
            A rule only starts working after you run <b>Bordereau Setup</b> for a program. Add or edit
            rules here first, then run Bordereau Setup.
          </div>
          <div style={{ marginTop: 6 }}>
            If a program is already set up, it won't use a new rule until you run its setup again.
          </div>
        </div>

        <div className="card">
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Rule</th><th>Type</th><th>Severity</th><th>Status</th><th></th></tr>
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
                      <td className="r">
                        <span className="linkish" onClick={() => nav(`/rule-library/${r.id}/edit`)}>Edit</span>
                        {" · "}
                        <span className="linkish" aria-disabled={busyId === r.id}
                          onClick={() => busyId === r.id ? undefined : onToggle(r)}>
                          {r.is_active ? "Disable" : "Enable"}
                        </span>
                        {" · "}
                        <span className="linkish" title="Delete this rule"
                          onClick={() => setDelTarget(r)}>Delete</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!loading && rows.length === 0 && (
              <div className="empty">
                No rules yet. {platform ? "Add a platform-wide check" : "Create your first rule"} to get started.
              </div>
            )}
            {loading && <div className="empty">Loading…</div>}
          </div>
          <Pagination
            page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
            totalItems={total} onPageChange={setPage} noun="rules" />
        </div>

        <div className="note" style={{ marginTop: 14}}>
          Disabled rules stop running but are kept, so you can turn them back on any time. Changes take
          effect after you run Bordereau Setup again.
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
              Delete <b>{delTarget.rule_name}</b>? This can't be undone. Existing results are unaffected;
              the rule just won't run on future uploads.
            </div>
            <div className="m-f">
              <button className="btn" onClick={() => setDelTarget(null)} disabled={delBusy}>Cancel</button>
              <button className="btn pri" onClick={confirmDelete} disabled={delBusy}>
                {delBusy ? "Deleting…" : "Delete Rule"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
