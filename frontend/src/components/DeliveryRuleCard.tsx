/**
 * The programme's delivery rule — what holds a broker's file back from the
 * carrier, and what happens at the correction deadline.
 *
 * Driven by each rule's OWN severity: the carrier picks which severities hold
 * a file (critical by default) and edits a single rule's severity in the Rule
 * Library / setup as before. At the deadline the system acts by itself, so no
 * file can wait for ever: it either delivers the file marked "unresolved
 * exceptions" (recording what was open) or keeps it on hold and tells the
 * carrier the decision is now theirs.
 */
import { useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { InfoTip } from "./InfoTip";
import { getDeliveryRule, putDeliveryRule, type DeliveryRule } from "../api/submissions";

const SEVERITIES: [string, string][] = [
  ["critical", "Critical"], ["warning", "Warning"], ["info", "Info"],
];

export default function DeliveryRuleCard({ programId, canEdit }: {
  programId: number; canEdit: boolean;
}) {
  const [rule, setRule] = useState<DeliveryRule | null>(null);
  const [draft, setDraft] = useState<DeliveryRule | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    getDeliveryRule(programId).then((r) => { setRule(r); setDraft(r); }).catch(() => setRule(null));
  }, [programId]);

  if (!rule || !draft) return null;
  const dirty = JSON.stringify(rule) !== JSON.stringify(draft);

  const toggleSev = (s: string) => setDraft({
    ...draft,
    hold_severities: draft.hold_severities.includes(s)
      ? draft.hold_severities.filter((x) => x !== s)
      : [...draft.hold_severities, s],
  });

  async function save() {
    if (!draft) return;
    setBusy(true); setMsg(null);
    try {
      const r = await putDeliveryRule(programId, {
        hold_severities: draft.hold_severities,
        correction_days: draft.correction_days,
        deadline_action: draft.deadline_action,
      });
      setRule(r); setDraft(r);
      setMsg({ ok: true, text: "Saved. Applies to files checked from now on." });
    } catch (e) {
      const d = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setMsg({ ok: false, text: d || "Could not save the rule." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-border bg-white p-4">
      <div className="mb-3 flex items-center gap-2">
        <ShieldCheck size={16} className="text-navy" />
        <h3 className="text-sm font-semibold text-ink">Exception Delivery Rule</h3>
        <InfoTip text={"A broker's file reaches you once the exceptions this rule holds back are fixed "
          + "or answered by the broker. Each check's own severity decides. At the deadline the system "
          + "acts automatically, so a file never waits for ever."} />
      </div>
      <div className="grid gap-4 text-sm md:grid-cols-3">
        <div>
          <div className="mb-1.5 text-xs font-medium text-ink-muted">Hold the file for</div>
          <div className="flex flex-wrap gap-2">
            {SEVERITIES.map(([k, label]) => (
              <label key={k} className="inline-flex items-center gap-1.5">
                <input type="checkbox" disabled={!canEdit || busy}
                       checked={draft.hold_severities.includes(k)} onChange={() => toggleSev(k)} />
                {label}
              </label>))}
          </div>
          <div className="mt-1 text-xs text-ink-soft">
            {draft.hold_severities.length ? "Other exceptions travel with the file." : "Nothing holds the file."}
          </div>
        </div>
        <div>
          <div className="mb-1.5 text-xs font-medium text-ink-muted">Days the broker has to fix</div>
          <input type="number" min={1} max={60} disabled={!canEdit || busy}
                 value={draft.correction_days}
                 onChange={(e) => setDraft({ ...draft, correction_days: Number(e.target.value) || 1 })}
                 className="w-24 rounded-md border border-border px-2 py-1.5" />
        </div>
        <div>
          <div className="mb-1.5 text-xs font-medium text-ink-muted">At the deadline</div>
          <select disabled={!canEdit || busy} value={draft.deadline_action}
                  onChange={(e) => setDraft({ ...draft,
                    deadline_action: e.target.value as DeliveryRule["deadline_action"] })}
                  className="w-full rounded-md border border-border px-2 py-1.5">
            <option value="deliver_flagged">Deliver it marked “unresolved exceptions”</option>
            <option value="keep_on_hold">Keep it on hold — I decide</option>
          </select>
        </div>
      </div>
      {canEdit && (
        <div className="mt-3 flex items-center gap-3">
          <button onClick={save} disabled={!dirty || busy}
                  className="rounded-md bg-navy px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
            {busy ? "Saving…" : "Save Rule"}</button>
          {msg && <span className={`text-xs ${msg.ok ? "text-success" : "text-warn"}`}>{msg.text}</span>}
        </div>)}
    </div>
  );
}
