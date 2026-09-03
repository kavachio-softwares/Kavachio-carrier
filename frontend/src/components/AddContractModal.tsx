/**
 * Add a contract for ONE broker, from the broker's own page.
 *
 * Same job the Bordereau Setup builder does, minus everything that is about the
 * bordereau: the file is read by the same extraction, against the same output
 * template, producing the same clauses and validation rules — which is the
 * point. A contract added here is a contract a setup can then be built on
 * without reading it a second time.
 *
 * A CONTRACT IS ADDED ON ITS OWN. No output template is required and none is
 * asked for: this screen adds a contract to a broker, nothing more. The clauses
 * are read and saved either way.
 *
 * What the template changes is how FAR the reading gets. Rules are written
 * against an output template's columns, so with one the clauses also become
 * compiled validation rules; without one the run stops after the clauses and
 * every rule-bearing clause is recorded as awaiting a template. The scope is
 * asked which template applies — and if it has one, it is used, because that
 * costs the user nothing and produces strictly more. It is never a gate.
 */
import { useEffect, useMemo, useState } from "react";
import {
  FileText, Loader2, AlertTriangle, ExternalLink, CheckCircle2,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { currentMga } from "../auth";
import {
  uploadContract, type ContractUploadCounts, type ExternalReference,
} from "../api/contracts";
import { errText, scheduleOf } from "../utils/directSetup";
import {
  resolveOutputTemplate, type ResolveResult,
} from "../api/outputTemplate";
import { FileDrop, MultiFileDrop } from "./ui/FileDrop";
import Modal from "./ui/Modal";
import { Button } from "./ui/Button";
import { Field, Select } from "./ui/Field";

type Programme = { id: number; name: string; status: string };

export default function AddContractModal({
  open, onClose, broker, programmes, onAdded,
}: {
  open: boolean;
  onClose: () => void;
  broker: { id: number; legal_name: string };
  /** The broker's programmes with this carrier. A contract is (programme ×
   *  broker), so one of these is what it will be filed under. */
  programmes: Programme[];
  /** Called once the contract is saved, with where it landed. The dialog closes
   *  itself afterwards unless it still has something to report, so this should
   *  refresh the page rather than close it. */
  onAdded: (contractId: number, programId: number) => void;
}) {
  const mga = currentMga();

  // Only a programme the broker is still ON can take a new contract — the
  // server refuses the rest, so they are not offered.
  const live = useMemo(
    () => programmes.filter(p => p.status !== "inactive"), [programmes]);

  const [programId, setProgramId] = useState<number | "">("");
  const [carrierId, setCarrierId] = useState<number | "">("");
  const [file, setFile] = useState<File | null>(null);
  const [refFiles, setRefFiles] = useState<File[]>([]);

  const [resolved, setResolved] = useState<ResolveResult | null>(null);
  const [resolving, setResolving] = useState(false);

  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState("");
  const [err, setErr] = useState<string | null>(null);
  // The pause: the contract defers rule content to a document nobody supplied.
  const [halt, setHalt] = useState<
    { refs: ExternalReference[]; resumeToken: string | null } | null>(null);
  // What the upload wrote. Set once it lands, and the dialog then STAYS open on
  // it: the contract is the thing being added here, so "it was added, and this
  // is what came out of it" is the answer to the question the screen was opened
  // to ask. Closing on success put that answer behind the dialog.
  const [saved, setSaved] = useState<
    { cid: number; deferred: string[]; counts: ContractUploadCounts | null } | null>(null);

  // A fresh open starts clean: a file staged for one broker must never ride
  // along into the next.
  useEffect(() => {
    if (open) return;
    setProgramId(""); setFile(null); setRefFiles([]);
    setHalt(null); setSaved(null); setErr(null); setStep("");
  }, [open]);

  // One programme means there is nothing to choose — choose it.
  useEffect(() => {
    if (!open) return;
    if (programId === "" && live.length === 1) setProgramId(live[0].id);
  }, [open, live, programId]);

  // Who this carrier is. Resolved, never asked: the signed-in seat IS the
  // carrier, and the template ladder is scoped by it.
  useEffect(() => {
    if (!open) return;
    api.get<{ id: number; legal_name: string }>(`/my-carrier-party`, { params: { mga } })
      .then(r => setCarrierId(r.data.id))
      .catch(() => setCarrierId(""));
  }, [open, mga]);

  // Which output template this exact scope points at — the server's answer,
  // never a guess assembled here, because it also reports how specific the
  // match was ("this is the programme's template, not this broker's").
  useEffect(() => {
    if (!open || programId === "") { setResolved(null); return; }
    let stale = false;
    setResolving(true);
    resolveOutputTemplate(mga, {
      program_id: Number(programId),
      carrier_party_id: carrierId === "" ? null : Number(carrierId),
      broker_party_id: broker.id,
    })
      .then(r => { if (!stale) setResolved(r); })
      .catch(() => { if (!stale) setResolved(null); })
      .finally(() => { if (!stale) setResolving(false); });
    return () => { stale = true; };
  }, [open, mga, programId, carrierId, broker.id]);

  // Used when the scope has one; null is a perfectly good answer — it decides
  // how far the reading gets, not whether it happens.
  const templateId: number | null = resolved?.template?.id ?? null;

  const ready = programId !== "" && !!file && !busy;

  async function submit(opts?: { continueAnyway?: boolean; extraRefs?: File[] }) {
    // Deliberately NOT gated on a template: without one the contract is still
    // read and its clauses still saved — see the header.
    if (programId === "" || !file) return;
    const refs = [...refFiles, ...(opts?.extraRefs ?? [])];
    if (opts?.extraRefs?.length) setRefFiles(refs);
    setBusy(true); setErr(null); setHalt(null); setSaved(null);
    setStep(templateId == null
      ? `Reading “${file.name}” and saving its clauses…`
      : `Reading “${file.name}” and writing its rules…`);
    try {
      const res = await uploadContract({
        programId: Number(programId),
        outputTemplateId: templateId,
        file,
        brokerPartyId: broker.id,
        // Derived the same way the setup builder derives it, so a "Schedule H"
        // contract added here occupies the same slot it would have there —
        // rather than superseding the whole programme's contracts.
        scheduleKey: scheduleOf(file.name),
        referenceFiles: refs,
        enableReferenceHalt: true,
        continueAnyway: opts?.continueAnyway,
        resumeToken: opts?.continueAnyway ? halt?.resumeToken ?? null : null,
        onProgress: setStep,
      });
      if ("halt" in res) { setHalt(res.halt); return; }
      setSaved({ cid: res.cid, deferred: res.deferred, counts: res.counts });
      // Refreshes the page behind; the dialog closes when the user is done
      // reading what it produced.
      onAdded(res.cid, Number(programId));
    } catch (e: unknown) {
      setErr(errText(e));
    } finally { setBusy(false); setStep(""); }
  }

  const noProgrammes = live.length === 0;

  return (
    <Modal open={open} size="xl" onClose={busy ? () => {} : onClose}
      title={`Add a contract for ${broker.legal_name}`}
      footer={saved ? (
        // Nothing left to do here — the contract is added. Offering "Add
        // contract" again beside a contract that was just added is what made
        // this look as though it had failed.
        <Button onClick={onClose}>Done</Button>
      ) : (
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            {busy ? "Working…" : "Cancel"}
          </Button>
          <Button onClick={() => submit()} disabled={!ready || !!halt}>
            {busy ? <><Loader2 size={14} className="animate-spin" /> Reading…</>
                  : "Add contract"}
          </Button>
        </>
      )}>
      <div className="space-y-4">
        {saved ? (
          <SavedPanel saved={saved} programId={Number(programId)}
            filename={file?.name ?? null} />
        ) : (
        <>
        <p className="text-[12.5px] text-ink-muted">
          The contract is read here exactly as it is in Bordereau Setup — the
          document is parsed and its clauses extracted and saved. A contract
          added here can be used by a setup later without being read again.
        </p>

        {noProgrammes ? (
          <Note tone="warn">
            {broker.legal_name} is not on any of your programmes yet, and a
            contract belongs to a programme. Put them on one first — until then
            there is nothing for a contract to sit under.
          </Note>
        ) : (
          <>
            <Field label="Programme">
              <Select value={programId} disabled={busy || live.length === 1}
                onChange={e => setProgramId(e.target.value ? Number(e.target.value) : "")}>
                <option value="">Choose a programme…</option>
                {live.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </Select>
            </Field>

            {/* What this reading will produce, given what the scope has. Never a
                gate — the contract is added either way. */}
            {programId !== "" && (
              <TemplateState resolving={resolving} resolved={resolved} />
            )}

            {/* The same dashed box Bordereau Setup uses — one component, so a
                document is asked for the same way wherever you are. */}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 items-start">
              <FileDrop
                label="Contract" required tone="required"
                icon={<FileText size={15} />}
                accept=".pdf,.docx"
                file={file}
                onPick={f => { setFile(f); setHalt(null); }}
                hint="The signed contract, as a PDF or Word file"
                disabled={busy || programId === ""}
                disabledNote={programId === "" ? "Choose a programme first" : "Reading…"} />
              <MultiFileDrop
                label="Reference Document(s)" tone="optional"
                icon={<FileText size={15} />}
                accept=".pdf,.docx,.doc,.txt,.xlsx,.xls,.csv"
                files={refFiles}
                onChange={setRefFiles}
                hint="Guidelines the contract defers to (e.g. Purchasing Guidelines)"
                disabled={busy || programId === ""}
                disabledNote={programId === "" ? "Choose a programme first" : "Reading…"} />
            </div>

            {/* What this upload REPLACES, said before it happens. A contract
                whose name carries a schedule takes only that schedule's slot;
                one that doesn't is the broker's contract for the programme, and
                supersedes whatever they held before. Either way it never
                touches another broker's. */}
            {file && (
              <p className="text-[11.5px] text-ink-soft">
                {scheduleOf(file.name)
                  ? <>Read as <b>{scheduleOf(file.name)}</b> from the file name,
                      so it replaces only this broker's contract for that schedule.</>
                  : <>The file name names no schedule, so this becomes this
                      broker's contract for the programme and supersedes any
                      earlier one of theirs. Other brokers' contracts are
                      untouched.</>}
              </p>
            )}
          </>
        )}

        {busy && step && (
          <div className="flex items-start gap-2 rounded-md bg-surface-2 px-3 py-2 text-[12.5px] text-ink-muted">
            <Loader2 size={14} className="animate-spin mt-0.5 shrink-0" />
            <span>{step} This can take several minutes for a long contract — leave this open.</span>
          </div>
        )}

        {halt && (
          <HaltCard refs={halt.refs} disabled={busy}
            onAttach={fs => submit({ extraRefs: fs })}
            onContinue={() => submit({ continueAnyway: true })} />
        )}

        {err && <Note tone="error">{err}</Note>}
        </>
        )}
      </div>
    </Modal>
  );
}

/** What the upload actually produced — the answer the dialog was opened to get.
 *
 *  Every number here comes from the persister's own tally, never from a guess
 *  about what "should" have happened: `rules: 0` with 24 clauses awaiting a
 *  template is a correct, complete outcome for a contract added before any
 *  bordereau work exists, and it has to read as one rather than as a failure. */
function SavedPanel({ saved, programId, filename }: {
  saved: { cid: number; deferred: string[]; counts: ContractUploadCounts | null };
  programId: number;
  filename: string | null;
}) {
  const c = saved.counts;
  return (
    <div className="space-y-3">
      <div className="flex items-start gap-2 rounded-md bg-emerald-50 px-3 py-2.5">
        <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-emerald-600" />
        <div className="text-[13px] text-emerald-900">
          <div className="font-medium">Contract added</div>
          <div className="text-[12px] text-emerald-800 mt-0.5">
            {filename ? <><b>{filename}</b> was read and saved.</> : "Read and saved."}
          </div>
        </div>
      </div>

      {c && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat n={c.clauses} label="clauses" />
          <Stat n={c.rules} label={c.rules === 1 ? "rule" : "rules"} />
          <Stat n={c.review} label="awaiting a template" />
          <Stat n={c.control} label="control register" />
        </div>
      )}

      {c && c.rules === 0 && c.review > 0 && (
        <Note>
          No rules yet, which is expected without an output template — a rule is
          written against a template's columns. The {c.review} clause
          {c.review === 1 ? "" : "s"} that carry one are held, and a Bordereau
          Setup can pick this contract up without reading the document again.
        </Note>
      )}

      {saved.deferred.length > 0 && (
        <Note tone="warn">
          The contract refers to {saved.deferred.join(", ")}, which wasn't
          provided — anything it defers to those was not read. Add the document
          and upload the contract again if you need those clauses.
        </Note>
      )}

      <Link to={`/programs/${programId}/contracts/${saved.cid}`}
        className="inline-flex items-center gap-1.5 text-[12.5px] text-navy hover:underline">
        <FileText size={13} /> See everything this contract produced
      </Link>
    </div>
  );
}

/** One number and what it counts. */
function Stat({ n, label }: { n: number; label: string }) {
  return (
    <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
      <div className="text-[17px] font-semibold leading-none">{n}</div>
      <div className="text-[11px] text-ink-muted mt-1">{label}</div>
    </div>
  );
}

/** What this reading will produce. The template is reported, never requested:
 *  it decides whether the clauses also become rules now or wait for one. */
function TemplateState({ resolving, resolved }: {
  resolving: boolean;
  resolved: ResolveResult | null;
}) {
  if (resolving) {
    return <Note>Checking what this programme reports into…</Note>;
  }
  const t = resolved?.template;
  if (!t) {
    return (
      <Note>
        No output template on this programme yet, so this reads the contract and
        saves its <b>clauses</b>. Rules are written against a template's columns,
        so the clauses that carry one are held until a Bordereau Setup exists —
        the contract itself will not need reading again.
      </Note>
    );
  }
  const level = resolved?.match_level;
  return (
    <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
      <div className="text-[10.5px] uppercase tracking-wide text-ink-soft">
        Also read against
      </div>
      <div className="text-[13px] font-medium">
        {t.name} <span className="font-normal text-ink-muted">v{t.version}</span>
      </div>
      <p className="text-[11px] text-ink-soft mt-0.5">
        {level === "broker"
          ? "This broker's own output template"
          : level === "contract"
            ? "The template this scope's contract is bound to"
            : "The programme's output template"}
        {" — so the clauses become validation rules in the same pass."}
      </p>
    </div>
  );
}

/** Extraction paused: the contract defers to document(s) nobody supplied. Two
 *  honest ways forward, and no third. */
function HaltCard({ refs, onAttach, onContinue, disabled }: {
  refs: ExternalReference[];
  onAttach: (fs: File[]) => void;
  onContinue: () => void;
  disabled?: boolean;
}) {
  const names = refs.map(r => r.document_name).filter(Boolean) as string[];
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-[13px] font-medium text-amber-800">
        <AlertTriangle size={14} /> This contract refers to a document that wasn't provided
      </div>
      <ul className="mt-1 ml-5 list-disc text-[12px] text-amber-800">
        {names.length ? names.map((n, i) => <li key={i}>{n}</li>)
                      : <li>an external document it does not name</li>}
      </ul>
      <p className="text-[11.5px] text-amber-700 mt-1">
        Rules it defers to that document cannot be written without it.
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <label className={`inline-flex items-center gap-1.5 rounded-md border border-amber-400
          bg-white px-2.5 py-1 text-[12px] font-medium text-amber-800
          ${disabled ? "opacity-50" : "cursor-pointer hover:bg-amber-100"}`}>
          <ExternalLink size={12} /> Attach it and carry on
          <input type="file" multiple className="hidden" disabled={disabled}
            accept=".pdf,.docx,.doc,.txt,.xlsx,.xls,.csv"
            onChange={e => {
              const fs = Array.from(e.target.files || []);
              e.target.value = "";
              if (fs.length) onAttach(fs);
            }} />
        </label>
        <button className="text-[12px] text-amber-800 underline disabled:opacity-50"
          disabled={disabled} onClick={onContinue}>
          Continue without it
        </button>
      </div>
    </div>
  );
}

function Note({ children, tone }: {
  children: React.ReactNode; tone?: "warn" | "error";
}) {
  const cls = tone === "error" ? "bg-danger/10 text-danger"
    : tone === "warn" ? "bg-amber-50 text-amber-800"
    : "bg-surface-2 text-ink-muted";
  return <div className={`rounded-md px-3 py-2 text-[12.5px] ${cls}`}>{children}</div>;
}
