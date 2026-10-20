/**
 * A treaty's reinsurance bordereau setup, read-only — laid out as a broker's
 * saved Bordereau Setup is (BordereauSetupDetail): a header with Activate and
 * Edit, then tabs that each answer one question.
 *
 *   Setup Summary               the facts, and three tiles that open the tab
 *                               they count — the last one what to look at
 *   Treaty Rules                what every bordereau is checked against
 *   Map Input & Output Columns  each output column and the input that fills it
 *   Output BDX Template         the file each reinsurer receives
 *   Due Dates                   how often, when due, and each period's state
 *
 * Changing anything is Edit (ReinsuranceSetupEdit). A treaty with no setup
 * yet has nothing to show, so it goes straight there.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft, CalendarDays, FileSpreadsheet, FileUp, Info, ListChecks, Pencil,
  ShieldCheck, Unlink, Columns3, AlertTriangle,
} from "lucide-react";
import { PageBody, PageHeader } from "../components/Layout";
import Card from "../components/ui/Card";
import Button from "../components/ui/Button";
import { Modal } from "../components/ui/Modal";
import { SetupTabs, useSetupTab, type SetupTab } from "../components/SetupTabs";
import { fieldErrors } from "../api/contractRecord";
import {
  activateSetup, getSetup, listCeded, previewSetup, sendBackSetup,
  type CededSummary, type SetupPreview, type SetupRecord, type SetupValues,
} from "../api/ceded";
import { TreatyRulesStep } from "../components/treaty/TreatyRules";
import { FieldMappingStep, treatyRuleLine } from "../components/treaty/CededMappingSteps";
import OutputGrid from "../components/treaty/OutputGrid";
import { GapLine, JumpTile, Stat } from "../components/treaty/SetupTiles";
import { CEDED_STATUS } from "./ReinsuranceBordereaux";
import { fmtDate, fmtStamp } from "../utils/date";

const STATUS_LABEL: Record<string, string> = {
  active: "Active", pending_approval: "Awaiting approval", draft: "Draft",
};
const STATUS_TONE: Record<string, string> = {
  active: "bg-emerald-100 text-emerald-700", pending_approval: "bg-blue-50 text-blue-700",
  draft: "bg-amber-50 text-amber-700",
};
const FREQ: Record<string, string> = { monthly: "Monthly", quarterly: "Quarterly", annually: "Yearly" };

const TAB_KEYS: SetupTab[] = [
  { key: "overview", label: "" }, { key: "rules", label: "" }, { key: "mapping", label: "" },
  { key: "output", label: "" }, { key: "calendar", label: "" },
];

/** The input columns as the setup stands: its own file, else the programmes'. */
function inputColumns(rec: SetupRecord, v: SetupValues) {
  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  return own
    ? own.columns.map(c => ({ name: c.name, field: c.field }))
    : rec.options.inputs.filter(t => !t.own).flatMap(t => t.columns.map(c => ({ name: c.name, field: c.field })));
}

export default function ReinsuranceSetupView() {
  const { contractId } = useParams();
  const cid = Number(contractId);
  const nav = useNavigate();
  const [rec, setRec] = useState<SetupRecord | null>(null);
  const [pv, setPv] = useState<SetupPreview | null>(null);
  const [stmts, setStmts] = useState<CededSummary[] | null>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useSetupTab(TAB_KEYS);
  const [confirm, setConfirm] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [rejectNote, setRejectNote] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getSetup(cid).then(r => {
      setRec(r);
      if (r.setup) previewSetup(cid, r.setup).then(setPv).catch(() => setPv(null));
    }).catch(e => setErr(fieldErrors(e).message));
    listCeded(cid).then(d => setStmts(d.items.filter(i => i.contract_id === cid)))
      .catch(() => setStmts([]));
  }, [cid]);

  const v = rec?.setup ?? null;

  // What the tiles and tabs count.
  const facts = useMemo(() => {
    if (!rec || !v) return null;
    const cols = inputColumns(rec, v);
    const fieldsIn = new Set(cols.map(c => c.field).filter(Boolean) as string[]);
    const kept = v.columns.filter(c => c.keep !== false);
    const reads = (key: string): string[] => key.startsWith("field:") ? [key.slice(6)]
      : key.startsWith("col:") || key === "ceded_pct" ? [] : rec.options.derived[key]?.fields ?? [];
    const ownFile = v.input_layout?.source === "own";
    const names = new Set(cols.map(c => c.name));
    const blank = kept.filter(c => !c.key
      || (c.key.startsWith("col:") && !names.has(c.key.slice(4)))
      || (reads(c.key).length > 0 && !reads(c.key).some(f => fieldsIn.has(f)))
      || (ownFile && (c.key === "programme" || c.key === "broker")));
    const rules = (v.rules ?? []).filter(r => r.enabled);
    const untested = rules.filter(r => !(rec.options.rule_fields?.[r.subject] ?? [])
      .some(f => fieldsIn.has(f)));
    const noContact = rec.options.reinsurers.filter(r => !r.contacts.some(c => c.email));
    return { kept, blank, rules, untested, noContact,
             critical: rules.filter(r => r.severity === "critical").length };
  }, [rec, v]);

  if (err) return <PageBody><div className="note warn">{err}</div></PageBody>;
  if (!rec) return <PageBody><div className="text-sm text-ink-muted">Loading…</div></PageBody>;
  // Nothing set up yet — there is nothing to view, only to set up.
  if (!v || !facts) return <Navigate to={`/reinsurance/setups/${cid}/edit`} replace />;

  const status = v.status;
  const pending = status === "pending_approval";
  const editHref = `/reinsurance/setups/${cid}/edit`;
  const tabs: SetupTab[] = [
    { key: "overview", label: "Setup Summary",
      info: "The treaty, its templates and status, with counts that open the other tabs." },
    { key: "rules", label: "Treaty Rules", count: facts.rules.length,
      info: "What every reinsurance bordereau for this treaty is checked against." },
    { key: "mapping", label: "Map Input & Output Columns",
      info: "Each column of the reinsurer's file, and the input column that fills it." },
    { key: "output", label: "Output BDX Template",
      info: "The file each reinsurer receives." },
    { key: "calendar", label: "Due Dates",
      info: "How often a statement is made, when it is due, and where each period stands." },
  ];

  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  const inputName = own ? (own.filename ?? "Your input template")
    : `Brokers' bordereaux · ${(v.program_ids ?? rec.options.programmes.map(p => p.id)).length} programme(s)`;
  const lay = v.layout ?? { source: "kavachio" as const };
  const outputName = lay.source === "uploaded" ? (lay.filename ?? "Your output template")
    : lay.source === "standard" ? `Lloyd's risk sheet${lay.standard?.jurisdiction ? ` (${lay.standard.jurisdiction})` : ""}`
    : "Kavachio standard layout";
  const progs = rec.options.programmes.filter(p => v.program_ids == null || v.program_ids.includes(p.id));
  const toLook = facts.blank.length + facts.untested.length + facts.noContact.length;

  const sampleRows = (pv?.table?.rows ?? []).slice(0, 5);

  async function activate() {
    setBusy(true);
    try { setRec(await activateSetup(cid)); setConfirm(false); }
    catch (e) { setErr(fieldErrors(e).message); }
    finally { setBusy(false); }
  }
  async function sendBack() {
    setBusy(true);
    try { setRec(await sendBackSetup(cid, rejectNote.trim())); setRejecting(false); }
    catch (e) { setErr(fieldErrors(e).message); }
    finally { setBusy(false); }
  }

  const Fact = ({ label, children }: { label: string; children: ReactNode }) => (
    <div className="min-w-0">
      <div className="text-[11px] uppercase text-ink-muted mb-1">{label}</div>
      <div className="font-medium">{children}</div>
    </div>
  );

  return (
    <>
      <PageHeader title={`${rec.treaty} — Reinsurance bordereau`}
        subtitle={`${STATUS_LABEL[status] ?? status} · Last Modified ${fmtStamp(v.updated_at)}`}
        action={
          <div className="flex items-center gap-2">
            <Button variant="secondary" onClick={() => nav("/direct/setups?tab=reinsurance")}>
              <ArrowLeft size={15} /> Configured Bordereau Setups
            </Button>
            {status === "draft" && (
              <Button variant="secondary" disabled={busy} onClick={() => setConfirm(true)}>
                <ShieldCheck size={15} /> {rec.carrier_admin ? "Activate Setup" : "Send for Approval"}
              </Button>
            )}
            {pending && rec.carrier_admin && (
              <>
                <Button variant="secondary" disabled={busy}
                  onClick={() => { setRejectNote(""); setRejecting(true); }}>
                  <ArrowLeft size={15} /> Send Back
                </Button>
                <Button variant="secondary" disabled={busy} onClick={() => setConfirm(true)}>
                  <ShieldCheck size={15} /> Approve
                </Button>
              </>
            )}
            <Button onClick={() => nav(`${editHref}${tab === "overview" ? "" : `?tab=${tab}`}`)}>
              <Pencil size={15} /> Edit
            </Button>
          </div>
        } />
      <PageBody>
        <SetupTabs tabs={tabs} current={tab} onChange={setTab} />
        <p className="mb-3 flex items-center gap-2 rounded-md bg-surface-2 px-3 py-2 text-[12.5px] text-ink-muted">
          <Info size={14} className="shrink-0" /> Read-only. Press Edit to change anything.
        </p>
        {v.note && status === "draft" && (
          <div className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[12.5px] text-amber-800">
            <b>Sent back:</b> {v.note}
          </div>
        )}

        {tab === "overview" && (
          <>
            <Card title={<span className="flex items-center gap-2">
              <ShieldCheck size={16} className="text-navy" /> Setup Summary</span>}>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                <Fact label="Treaty">{rec.treaty}</Fact>
                <Fact label="Type">{rec.type_label ?? "—"}</Fact>
                <Fact label="Status">
                  <span className={`inline-flex text-[11px] rounded-full px-2 py-0.5 font-medium ${STATUS_TONE[status] ?? ""}`}>
                    {STATUS_LABEL[status] ?? status}
                  </span>
                </Fact>
                <Fact label="Last Modified">{fmtStamp(v.updated_at)}</Fact>
                <Fact label="Input Template">
                  <span className="flex items-center gap-1.5"><FileUp size={13} className="text-ink-soft shrink-0" />
                    <span className="truncate">{inputName}</span></span>
                </Fact>
                <Fact label="Output Template">
                  <span className="flex items-center gap-1.5"><FileSpreadsheet size={13} className="text-ink-soft shrink-0" />
                    <span className="truncate">{outputName}</span></span>
                </Fact>
                <Fact label="Treaty Rules">{facts.rules.length} Rule(s) · {facts.critical} Critical</Fact>
                <Fact label="Schedule">{FREQ[v.frequency]} · due {v.due_days} days after</Fact>
                <Fact label="Programmes">{progs.map(p => p.name).join(", ") || "—"}</Fact>
                <Fact label="Recipients">
                  {Object.values(v.recipients ?? {}).flat().length
                    ? `${Object.values(v.recipients).flat().length} Contact(s)` : "Every Contact"}
                </Fact>
                <Fact label="Sending">{v.send_mode === "auto" ? "Automatically" : "When You Press Send"}</Fact>
                <Fact label="Copied To">{v.cc?.length ? v.cc.join(", ") : "None"}</Fact>
              </div>
            </Card>

            {/* Where to go next — each tile opens the tab it counts. */}
            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <JumpTile n={facts.kept.length - facts.blank.length}
                label="output columns filled from the input" onClick={() => setTab("mapping")} />
              <JumpTile n={facts.rules.length} label="treaty rules check every bordereau"
                onClick={() => setTab("rules")} />
              <JumpTile n={toLook} warn={toLook > 0} label="things to look at"
                onClick={() => setTab(facts.blank.length ? "mapping" : facts.untested.length ? "rules" : "overview")}
                detail={toLook > 0 && <>
                  {facts.blank.length > 0 && <GapLine onClick={() => setTab("mapping")}>
                    {facts.blank.length} output column{facts.blank.length > 1 ? "s" : ""} no input fills</GapLine>}
                  {facts.untested.length > 0 && <GapLine onClick={() => setTab("rules")}>
                    {facts.untested.length} treaty rule{facts.untested.length > 1 ? "s" : ""} the input cannot test</GapLine>}
                  {facts.noContact.length > 0 && <GapLine onClick={() => nav(`${editHref}?tab=calendar`)}>
                    {facts.noContact.length} reinsurer{facts.noContact.length > 1 ? "s" : ""} with no email contact</GapLine>}
                </>} />
            </div>
          </>
        )}

        {tab === "rules" && (
          <Card title={<span className="flex items-center gap-2">
            <ListChecks size={16} className="text-navy" /> Treaty Rules
            <span className="text-xs font-normal text-ink-muted">· checked on every reinsurance bordereau for this treaty</span>
          </span>}>
            <div className="mb-4 grid gap-3 sm:grid-cols-4">
              <Stat icon={ListChecks} n={facts.rules.length - facts.untested.length} label="Rules check your file" />
              <Stat icon={Unlink} n={facts.untested.length} label="Rules with no column" warn={facts.untested.length > 0} />
              <Stat icon={AlertTriangle} n={facts.critical} label="Critical — stop sending" />
              <Stat icon={Info} n={facts.rules.length - facts.critical} label="Warning — sent anyway" />
            </div>
            <TreatyRulesStep cid={cid} rec={rec} v={v} change={() => {}} readOnly />
          </Card>
        )}

        {tab === "mapping" && (
          <>
            <p className="mb-3 flex items-center gap-2 rounded-md bg-surface-2 px-3 py-2 text-[12.5px] text-ink-muted">
              <ListChecks size={14} className="shrink-0" /> {treatyRuleLine(rec, v)}
            </p>
            <Card title={<span className="flex items-center gap-2">
              <Columns3 size={16} className="text-navy" /> Field Mapping &amp; Validation Rules
              <span className="text-xs font-normal text-ink-muted">· {inputName} → {outputName}</span>
            </span>}>
              <div className="proto" style={{ background: "transparent" }}>
                <FieldMappingStep rec={rec} v={v} pv={pv} change={() => {}} readOnly hideStrip />
              </div>
            </Card>
          </>
        )}

        {tab === "output" && (
          <Card title={<span className="flex items-center gap-2">
            <FileSpreadsheet size={16} className="text-navy" /> Output BDX · {outputName}
            <span className="text-xs font-normal text-ink-muted">{v.file_format === "csv" ? "CSV" : "Excel"}</span>
          </span>}>
            <p className="mb-1 flex flex-wrap items-center gap-2 text-[12.5px] text-ink-muted">
              The file each reinsurer receives — its share of every line, in delivery order, under the
              names the file will carry.
              <span className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ${STATUS_TONE[status] ?? ""}`}>
                {STATUS_LABEL[status] ?? status}</span>
            </p>
            <p className="mb-3 text-[12.5px] text-ink-muted">
              {lay.source === "uploaded" ? "Built from the template you uploaded — its own columns, filled in as they are."
                : lay.source === "standard" ? "Built from the Lloyd's risk sheet, with the treaty's reinsurance columns after it."
                : "Kavachio's reinsurance columns, chosen and named on the setup."}
              {v.file_format !== "csv" && " An Excel file also has a summary sheet with the reinsurer's share and totals."}
            </p>
            <OutputGrid headings={facts.kept.map(c => c.heading)} rows={sampleRows}
              sheet={lay.sheet_name || "Lines"} period={pv?.period ?? null} />
          </Card>
        )}

        {tab === "calendar" && (
          <Card title={<span className="flex items-center gap-2">
            <CalendarDays size={16} className="text-navy" /> Due Dates</span>}>
            <div className="mb-4 grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <Fact label="One Statement Every">{FREQ[v.frequency]}</Fact>
              <Fact label="Due">{v.due_days} days after the period ends</Fact>
              <Fact label="Sending">{v.send_mode === "auto" ? "Automatically, once every bordereau is in" : "When you press Send"}</Fact>
              <Fact label="Statements So Far">{stmts?.filter(s => s.status !== "superseded").length ?? "…"}</Fact>
            </div>
            {stmts && stmts.length > 0 ? (
              <div className="overflow-hidden rounded-md border border-border">
                <div className="grid grid-cols-4 gap-3 bg-surface-2 px-3 py-2 text-[11px] font-semibold uppercase text-ink-muted">
                  <span>Period</span><span>Due</span><span>Status</span><span>Sent</span>
                </div>
                {stmts.filter(s => s.status !== "superseded").map(s => (
                  <div key={s.id} role="button" tabIndex={0}
                    onClick={() => nav(`/reinsurance/bordereaux/${s.id}`)}
                    className="grid cursor-pointer grid-cols-4 gap-3 border-t border-border bg-white px-3 py-2 text-[13px] hover:bg-surface-2">
                    <span className="font-mono">{s.period}{s.version_no > 1 ? ` · v${s.version_no}` : ""}</span>
                    <span className={s.overdue ? "font-semibold text-danger" : ""}>
                      {s.due_on ? fmtDate(s.due_on) : "—"}{s.overdue ? " · overdue" : ""}</span>
                    <span>{CEDED_STATUS[s.status]?.label ?? s.status}</span>
                    <span className="text-ink-muted">{s.sent_at ? fmtStamp(s.sent_at) : "—"}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-ink-muted">No statement has been prepared for this treaty yet.</p>
            )}
          </Card>
        )}
      </PageBody>

      <Modal open={confirm} size="md"
        title={pending ? "Approve this setup?" : rec.carrier_admin ? "Activate this setup?" : "Send for approval?"}
        onClose={() => { if (!busy) setConfirm(false); }}
        footer={<div className="flex items-center justify-end gap-2">
          <Button variant="secondary" disabled={busy} onClick={() => setConfirm(false)}>Cancel</Button>
          <Button disabled={busy} onClick={activate}>
            {busy ? "Working…" : pending || rec.carrier_admin ? "Activate" : "Send"}
          </Button>
        </div>}>
        <p className="text-sm text-ink-muted">
          {rec.carrier_admin
            ? "From now on a statement is prepared for each reinsurer every period, read and checked as this setup says."
            : "Your carrier admin is asked to approve it. Nothing is prepared until they do."}
        </p>
      </Modal>
      <Modal open={rejecting} title="Send this setup back?" size="md"
        onClose={() => { if (!busy) setRejecting(false); }}
        footer={<div className="flex items-center justify-end gap-2">
          <Button variant="secondary" disabled={busy} onClick={() => setRejecting(false)}>Cancel</Button>
          <Button disabled={busy || !rejectNote.trim()} onClick={sendBack}>
            {busy ? "Sending back…" : "Send back"}</Button>
        </div>}>
        <div className="text-sm text-ink-muted space-y-3">
          <p>It goes back to <strong className="text-ink">Draft</strong>, and whoever built it is told why.</p>
          <textarea className="input" rows={4} value={rejectNote}
            onChange={e => setRejectNote(e.target.value)}
            placeholder="The output template is last year's — use the new one and send it back up." />
        </div>
      </Modal>
    </>
  );
}
