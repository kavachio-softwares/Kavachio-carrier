// Programme Rules — the carrier's tab on Configure Program (pages/AddProgram).
// Rules set once for the whole programme, for all or chosen broker contracts
// and/or treaties; each goes to the brokers and/or reinsurers who must
// approve, and checks a party's files once that party approves
// (backend programme_rules.py). Written for people who are not technical: a
// rule is one of the questions a contract's terms ask, in the same words.
import { Fragment, useEffect, useMemo, useState } from "react";
import { Archive, ChevronDown, ChevronRight, Pencil, Plus, Send, Trash2 } from "lucide-react";
import {
  addProgrammeRule, deleteProgrammeRule, editProgrammeRule, getProgrammeRules, retireProgrammeRule,
  sendProgrammeRule,
  type ProgrammeRule, type ProgrammeRulesPage, type RuleApprover, type RuleCheck, type RuleInput,
  type RuleScope, type RuleSeverity,
} from "../api/programmeRules";
import { InfoTip } from "./InfoTip";
import { showDone, showError } from "./ResultPopup";
import { Modal } from "./ui/Modal";

const SEVERITY: { key: RuleSeverity; label: string; cls: string }[] = [
  { key: "critical", label: "Critical", cls: "bg-red-50 text-red-700" },
  { key: "warning", label: "Warning", cls: "bg-amber-50 text-amber-700" },
  { key: "info", label: "Info", cls: "bg-sky-50 text-sky-700" },
];
const APPROVER: Record<RuleApprover, string> = { broker: "Broker", reinsurer: "Reinsurer", both: "Broker and reinsurer" };

function statusClass(r: ProgrammeRule): string {
  if (r.status === "retired") return "bg-surface-2 text-ink-muted";
  if (r.status === "draft") return "bg-surface-2 text-ink";
  if (r.status_line.startsWith("Changes")) return "bg-amber-50 text-amber-700";
  if (r.status_line === "Active") return "bg-emerald-50 text-emerald-700";
  return "bg-sky-50 text-sky-700";
}

function scopeText(scope: RuleScope, ids: number[], all: string, one: string): string | null {
  if (scope === "all") return all;
  if (scope === "selected") return `${ids.length} ${one}${ids.length === 1 ? "" : "s"}`;
  return null;
}

const blank = (): RuleInput => ({
  name: "", check: "", operand: "", severity: "warning",
  broker_scope: "all", broker_contract_ids: [], treaty_scope: "none", treaty_contract_ids: [],
  approval_from: "broker",
});

const asked = (a: RuleApprover) => a === "broker"
  ? "The brokers of these contracts will be asked to approve it. It checks their files once they do."
  : a === "reinsurer" ? "The reinsurers on these treaties will be emailed to approve it. It checks their statements once they do."
  : "The brokers and reinsurers will be asked to approve it. It checks each one's files once they do.";

export default function ProgrammeRulesPanel({ programId }: { programId: number }) {
  const [page, setPage] = useState<ProgrammeRulesPage | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [form, setForm] = useState<{ id: number | null; body: RuleInput } | null>(null);
  const [confirm, setConfirm] = useState<{ rule: ProgrammeRule; what: "send" | "retire" | "delete" } | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getProgrammeRules(programId).then(setPage)
      .catch(e => setFailed(e?.response?.data?.detail ?? "Could not load the programme rules."));
  }, [programId]);

  const sentWords = (p: ProgrammeRulesPage) => p.sent
    ? [p.sent.brokers ? `${p.sent.brokers} broker${p.sent.brokers === 1 ? "" : "s"}` : "",
       p.sent.reinsurers ? `${p.sent.reinsurers} reinsurer${p.sent.reinsurers === 1 ? "" : "s"}` : ""]
      .filter(Boolean).join(" and ") : "";

  async function act() {
    if (!confirm) return;
    const { rule, what } = confirm;
    setBusy(true);
    try {
      const p = what === "send" ? await sendProgrammeRule(programId, rule.id)
        : what === "retire" ? await retireProgrammeRule(programId, rule.id) : await deleteProgrammeRule(programId, rule.id);
      setPage(p);
      setConfirm(null);
      showDone(what === "send" ? `Sent for approval${sentWords(p) ? ` to ${sentWords(p)}` : ""}.`
        : what === "retire" ? "Retired. It no longer checks anything." : "Deleted.");
    } catch (e) { showError(e, "That did not work"); }
    finally { setBusy(false); }
  }

  async function save(body: RuleInput, send: boolean) {
    if (!form) return;
    setBusy(true);
    try {
      let p = form.id == null ? await addProgrammeRule(programId, body) : await editProgrammeRule(programId, form.id, body);
      if (send) {
        // A new rule is the newest one on the programme.
        const id = form.id ?? Math.max(...p.rules.map(r => r.id));
        p = await sendProgrammeRule(programId, id);
      }
      setPage(p);
      setForm(null);
      showDone(send ? `Saved and sent for approval${sentWords(p) ? ` to ${sentWords(p)}` : ""}.`
        : "Saved as a draft. Send it for approval when it is ready.");
    } catch (e) { showError(e, "That did not work"); }
    finally { setBusy(false); }
  }

  if (failed) return <p className="text-sm text-ink-muted">{failed}</p>;
  if (!page) return <p className="text-sm text-ink-muted">Loading…</p>;
  if (page.ready === false) return <p className="text-sm text-ink-muted">{page.message}</p>;

  const addButton = (
    <button type="button" onClick={() => setForm({ id: null, body: blank() })}
      className="inline-flex items-center gap-1.5 rounded bg-navy px-3 py-1.5 text-sm font-medium text-white hover:opacity-90">
      <Plus size={15} /> Add rule
    </button>);

  return (
    <div>
      {page.rules.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border px-6 py-8 text-center">
          <p className="font-medium">No programme rules yet</p>
          <p className="mx-auto mt-1 max-w-md text-sm text-ink-muted">
            Add a rule once and it checks every file on this programme, after the broker or reinsurer approves it.
          </p>
          <div className="mt-4">{addButton}</div>
        </div>
      ) : (<>
        <div className="mb-3 flex items-center justify-between gap-3">
          <p className="flex items-center gap-1 text-sm text-ink-muted">
            {page.rules.length} rule{page.rules.length === 1 ? "" : "s"}
            <InfoTip text={"A rule checks a broker's or reinsurer's files once they approve it.\nChanging a sent rule asks them to approve it again."} />
          </p>
          {addButton}
        </div>
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-left text-sm">
            <thead className="bg-surface-2 text-[11px] font-bold uppercase tracking-wide text-ink-muted">
              <tr>
                <th className="w-6" />
                <th className="px-3 py-2 text-left">Rule</th>
                <th className="px-3 py-2 text-left">Applies to</th>
                <th className="px-3 py-2 text-left">Approved by</th>
                <th className="px-3 py-2 text-left">Severity</th>
                <th className="px-3 py-2 text-left">Status</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {page.rules.map(r => {
                const sev = SEVERITY.find(x => x.key === r.severity)!;
                const applies = [scopeText(r.broker_scope, r.broker_contract_ids, "All broker contracts", "broker contract"),
                                 scopeText(r.treaty_scope, r.treaty_contract_ids, "All treaties", "treaty")]
                  .filter(Boolean).join(" · ");
                const isOpen = open === r.id;
                return (
                  <Fragment key={r.id}>
                    <tr className="border-t border-border align-top">
                      <td className="py-2.5 pl-2">
                        {r.approvals.length > 0 && (
                          <button type="button" onClick={() => setOpen(isOpen ? null : r.id)}
                            title={isOpen ? "Hide who answered" : "See who answered"} className="text-ink-muted">
                            {isOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                          </button>)}
                      </td>
                      <td className="px-3 py-2.5 text-left">
                        <div className="font-medium">{r.name}</div>
                        {r.sentence !== r.name && <div className="text-xs text-ink-muted">{r.sentence}</div>}
                      </td>
                      <td className="px-3 py-2.5 text-left">{applies}</td>
                      <td className="px-3 py-2.5 text-left">{APPROVER[r.approval_from]}</td>
                      <td className="px-3 py-2.5 text-left">
                        <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${sev.cls}`}>{sev.label}</span>
                      </td>
                      <td className="px-3 py-2.5 text-left">
                        <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${statusClass(r)}`}>{r.status_line}</span>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2.5 text-right">
                        {r.status === "draft" && (
                          <button type="button" className="mr-1 inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-xs hover:bg-surface-2"
                            onClick={() => setConfirm({ rule: r, what: "send" })}><Send size={13} /> Send for approval</button>)}
                        {r.status !== "retired" && (
                          <button type="button" title="Edit" className="mr-1 rounded p-1 text-ink-muted hover:bg-surface-2"
                            onClick={() => setForm({ id: r.id, body: {
                              name: r.name, check: r.check,
                              operand: Array.isArray(r.operand) ? r.operand.join(", ") : r.operand ?? "",
                              severity: r.severity, broker_scope: r.broker_scope,
                              broker_contract_ids: r.broker_contract_ids, treaty_scope: r.treaty_scope,
                              treaty_contract_ids: r.treaty_contract_ids, approval_from: r.approval_from } })}>
                            <Pencil size={15} /></button>)}
                        {r.status === "sent" && (
                          <button type="button" title="Retire — stop checking with it" className="mr-1 rounded p-1 text-ink-muted hover:bg-surface-2"
                            onClick={() => setConfirm({ rule: r, what: "retire" })}><Archive size={15} /></button>)}
                        {r.status === "draft" && !r.sent_at && (
                          <button type="button" title="Delete" className="rounded p-1 text-ink-muted hover:bg-surface-2"
                            onClick={() => setConfirm({ rule: r, what: "delete" })}><Trash2 size={15} /></button>)}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="bg-surface-2/50">
                        <td />
                        <td colSpan={6} className="px-3 pb-3 text-left">
                          {r.approvals.map(a => (
                            <div key={`${a.kind}${a.party_id}`} className="flex flex-wrap items-center gap-2 py-1 text-sm">
                              <span className="font-medium">{a.party}</span>
                              <span className="text-xs text-ink-muted">{a.kind === "broker" ? "Broker" : "Reinsurer"}</span>
                              <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                                a.status === "approved" ? "bg-emerald-50 text-emerald-700"
                                : a.status === "changes_requested" ? "bg-amber-50 text-amber-700" : "bg-surface-2 text-ink-muted"}`}>
                                {a.status === "approved" ? "Approved" : a.status === "changes_requested" ? "Asked for changes" : "Waiting"}</span>
                              {a.note && <span className="text-ink-muted">“{a.note}”</span>}
                            </div>))}
                        </td>
                      </tr>)}
                  </Fragment>);
              })}
            </tbody>
          </table>
        </div>
      </>)}

      {form && <RuleForm page={page} editing={form.id != null} start={form.body} busy={busy}
        onClose={() => setForm(null)} onSave={save} />}

      <Modal open={!!confirm} onClose={() => setConfirm(null)} size="md"
        title={confirm?.what === "send" ? "Send for approval?" : confirm?.what === "retire" ? "Retire this rule?" : "Delete this draft?"}
        footer={<div className="flex justify-end gap-2">
          <button type="button" className="rounded border border-border px-3 py-1.5 text-sm" onClick={() => setConfirm(null)}>Cancel</button>
          <button type="button" disabled={busy} onClick={act}
            className={`rounded px-3 py-1.5 text-sm font-medium text-white ${confirm?.what === "send" ? "bg-navy" : "bg-red-600"}`}>
            {busy ? "Working…" : confirm?.what === "send" ? "Send" : confirm?.what === "retire" ? "Retire" : "Delete"}</button>
        </div>}>
        {confirm && <p className="text-sm text-ink">
          <b>{confirm.rule.sentence}</b><br />
          {confirm.what === "send" ? asked(confirm.rule.approval_from)
            : confirm.what === "retire" ? "It stops checking files straight away." : "This draft will be removed."}
        </p>}
      </Modal>
    </div>
  );
}

function RuleForm({ page, editing, start, busy, onClose, onSave }: {
  page: ProgrammeRulesPage; editing: boolean; start: RuleInput; busy: boolean;
  onClose: () => void; onSave: (b: RuleInput, send: boolean) => void;
}) {
  const [b, setB] = useState<RuleInput>(start);
  const set = (k: Partial<RuleInput>) => setB(x => ({ ...x, ...k }));
  const check: RuleCheck | undefined = page.checks.find(c => c.key === b.check);
  const groups = useMemo(() => {
    const m = new Map<string, RuleCheck[]>();
    for (const c of page.checks) m.set(c.group, [...(m.get(c.group) ?? []), c]);
    return [...m.entries()];
  }, [page.checks]);
  const { broker: brokerChoices, treaty: treatyChoices } = page.contracts;
  const onBrokers = b.broker_scope !== "none";
  const onTreaties = b.treaty_scope !== "none";
  // A question a treaty cannot check is offered for broker contracts only.
  const treatyBlocked = !!check && !check.treaty;
  const pick = (ids: number[], id: number, on: boolean) => (on ? [...ids, id] : ids.filter(x => x !== id));

  // Who approves follows the contracts picked; asked only when it is both.
  const approver: RuleApprover = onBrokers && onTreaties ? b.approval_from : onTreaties ? "reinsurer" : "broker";
  const unit = check?.value === "percent" ? "%" : check?.value === "months" ? " months" : "";
  const preview = !check ? "" : check.value == null ? check.label
    : `${check.label}: ${String(b.operand ?? "").trim() || "…"}${unit}`;
  const valueOk = !!check && (check.value == null || String(b.operand ?? "").trim() !== "");
  const contractsOk = (onBrokers || onTreaties)
    && (b.broker_scope !== "selected" || b.broker_contract_ids.length > 0)
    && (b.treaty_scope !== "selected" || b.treaty_contract_ids.length > 0)
    && !(onTreaties && treatyBlocked);
  const ready = valueOk && contractsOk;
  const out = (): RuleInput => ({
    ...b, approval_from: approver,
    operand: check?.value === "list" && typeof b.operand === "string"
      ? b.operand.split(/,|;/).map(x => x.trim()).filter(Boolean) : b.operand });

  const label = "mb-1.5 block text-sm font-semibold";
  const input = "w-full rounded border border-border px-2.5 py-2 text-sm";

  return (
    <Modal open onClose={onClose} size="xl" title={editing ? "Edit programme rule" : "Add programme rule"}
      footer={<div className="flex items-center justify-between gap-3">
        <span className="text-xs text-ink-muted">{editing && "Changing a sent rule asks everyone to approve it again."}</span>
        <div className="flex gap-2">
          <button type="button" className="rounded border border-border px-3 py-1.5 text-sm" onClick={onClose}>Cancel</button>
          <button type="button" disabled={busy || !ready} onClick={() => onSave(out(), false)}
            className="rounded border border-border px-3 py-1.5 text-sm disabled:opacity-50">Save as draft</button>
          <button type="button" disabled={busy || !ready} onClick={() => onSave(out(), true)}
            className="rounded bg-navy px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
            {busy ? "Saving…" : "Save and send for approval"}</button>
        </div>
      </div>}>
      <div className="grid gap-5">
        {/* 1. What it checks, and the answer */}
        <div>
          <label className={label}>What should this rule check?</label>
          <select className={input} value={b.check} onChange={e => set({ check: e.target.value, operand: "" })}>
            <option value="">Choose…</option>
            {groups.map(([g, cs]) => (
              <optgroup key={g} label={g}>
                {cs.map(c => <option key={c.key} value={c.key}>{c.label}</option>)}
              </optgroup>))}
          </select>
          <p className="mt-1 text-xs text-ink-muted">The same questions as a contract's agreed terms.</p>
        </div>
        {check?.value != null && (
          <div>
            <label className={label}>
              {check.value === "amount" ? "Amount" : check.value === "percent" ? "Percent"
                : check.value === "months" ? "Number of months" : check.value === "list" ? "Values"
                : check.key === "currency" ? "Currency" : "Value"}
            </label>
            <div className="flex items-center gap-2">
              <input className={input} value={String(b.operand ?? "")} onChange={e => set({ operand: e.target.value })}
                inputMode={check.value === "list" || check.value === "text" ? "text" : "decimal"}
                placeholder={check.value === "amount" ? "e.g. 5,000,000" : check.value === "percent" ? "e.g. 15"
                  : check.value === "months" ? "e.g. 12" : check.value === "list" ? "e.g. Cuba, Iran, Syria" : "e.g. USD"} />
              {check.value === "percent" && <span className="text-sm text-ink-muted">%</span>}
            </div>
            {check.value === "list" && <p className="mt-1 text-xs text-ink-muted">Separate with commas.</p>}
          </div>)}
        {check && (
          <div className="rounded bg-surface-2 px-3 py-2 text-sm">
            <span className="text-ink-muted">The rule will read: </span><b>{preview}</b>
          </div>)}

        {/* 2. How serious */}
        <div>
          <label className={`${label} flex items-center gap-1`}>How serious is it?
            <InfoTip text={"Critical: must be fixed.\nWarning: worth checking.\nInfo: for your information."} /></label>
          <div className="flex gap-2">
            {SEVERITY.map(s => (
              <button key={s.key} type="button" onClick={() => set({ severity: s.key })}
                className={`rounded border px-3 py-1.5 text-sm ${b.severity === s.key ? "border-navy bg-navy/5 font-semibold" : "border-border"}`}>
                {s.label}</button>))}
          </div>
        </div>

        {/* 3. Which contracts — the treaties box only when the programme has some */}
        <div>
          <label className={label}>Which contracts should it check?</label>
          <div className="grid gap-2">
            {([["broker", "Broker contracts", brokerChoices, b.broker_scope, b.broker_contract_ids, false],
               ["treaty", "Reinsurance treaties", treatyChoices, b.treaty_scope, b.treaty_contract_ids, treatyBlocked]] as const)
              .filter(([side, , choices]) => side === "broker" || choices.length > 0)
              .map(([side, title, choices, scope, ids, blocked]) => {
                const on = scope !== "none";
                const setScope = (sc: RuleScope) => set(side === "broker" ? { broker_scope: sc } : { treaty_scope: sc });
                return (
                  <div key={side} className={`rounded border p-3 ${on ? "border-navy/40" : "border-border"} ${blocked ? "opacity-60" : ""}`}>
                    <label className="flex items-center gap-2 text-sm font-medium">
                      <input type="checkbox" checked={on} disabled={blocked || choices.length === 0}
                        onChange={e => setScope(e.target.checked ? "all" : "none")} />
                      {title} on this programme
                    </label>
                    {blocked && <p className="ml-6 mt-1 text-xs text-ink-muted">This can't be checked on reinsurance treaties.</p>}
                    {!blocked && choices.length === 0 && <p className="ml-6 mt-1 text-xs text-ink-muted">None on this programme yet.</p>}
                    {on && (
                      <div className="ml-6 mt-2 flex flex-wrap gap-4 text-sm">
                        <label className="flex items-center gap-1.5">
                          <input type="radio" checked={scope === "all"} onChange={() => setScope("all")} />
                          All ({choices.length})</label>
                        <label className="flex items-center gap-1.5">
                          <input type="radio" checked={scope === "selected"} onChange={() => setScope("selected")} />
                          Only some</label>
                      </div>)}
                    {scope === "selected" && (
                      <div className="ml-6 mt-2 max-h-40 overflow-y-auto rounded border border-border p-2">
                        {choices.map(c => (
                          <label key={c.id} className="flex items-start gap-2 py-0.5 text-sm">
                            <input type="checkbox" className="mt-1" checked={ids.includes(c.id)}
                              onChange={e => set(side === "broker"
                                ? { broker_contract_ids: pick(b.broker_contract_ids, c.id, e.target.checked) }
                                : { treaty_contract_ids: pick(b.treaty_contract_ids, c.id, e.target.checked) })} />
                            <span>{c.name}<span className="block text-xs text-ink-muted">{c.party}</span></span>
                          </label>))}
                      </div>)}
                  </div>);
              })}
          </div>
        </div>

        {/* 4. Who approves — a choice only when both kinds are picked */}
        {(onBrokers || onTreaties) && (
          <div>
            <label className={label}>Who must approve it?</label>
            {onBrokers && onTreaties ? (
              <div className="flex flex-wrap gap-2">
                {(["broker", "reinsurer", "both"] as RuleApprover[]).map(a => (
                  <button key={a} type="button" onClick={() => set({ approval_from: a })}
                    className={`rounded border px-3 py-1.5 text-sm ${b.approval_from === a ? "border-navy bg-navy/5 font-semibold" : "border-border"}`}>
                    {APPROVER[a]}</button>))}
              </div>
            ) : <p className="text-sm text-ink-muted">{asked(approver)}</p>}
          </div>)}

        {/* 5. A name, if they want one */}
        <div>
          <label className={label}>Rule name <span className="font-normal text-ink-muted">(optional)</span></label>
          <input className={input} value={b.name} onChange={e => set({ name: e.target.value })}
            placeholder={check?.label ?? "e.g. Maximum sum insured"} />
        </div>
      </div>
    </Modal>
  );
}
