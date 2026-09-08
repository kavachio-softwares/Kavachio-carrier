/**
 * Upload Contract — the BROKER's side of contract creation, per the design's
 * v-b-upload screen. Step A of the flow: the broker brings a contract,
 * assigned to itself, under a programme a carrier has put it on — and then
 * waits, because nothing a broker uploads becomes usable on its own say-so.
 *
 * The design's layout, kept: Carrier (read-only, derived from the programme) ·
 * Programme (only ones you are assigned to) · Broker (read-only — you), then
 * the contract's name and term, the document, and "What happens after you
 * submit" alongside, ending in the warning note that names the rule the whole
 * screen exists under.
 *
 * What SUBMIT does, in order:
 *   1. creates the contract record (a broker's contract is born
 *      pending_approval — the server and a DB trigger both see to that);
 *   2. attaches the document, if one was given;
 *   3. submits it into the carrier's Approvals queue.
 * Step 1 succeeding while 2 or 3 fails leaves a real contract in Drafts rather
 * than nothing — the failure says so and where to finish the job.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ArrowLeft, Loader2, Send } from "lucide-react";
import { Dropzone } from "../components/Dropzone";
import {
  getBrokerDashboard, getBrokerProgrammes, type BrokerProgramme,
} from "../api/broker";
import {
  createContract, fieldErrors, getContractTypes, submitContract,
  uploadDocument,
  type ContractField, type ContractTypeSpec, type FieldErrors,
} from "../api/contractRecord";

const PLACEHOLDER: Record<string, string> = {
  name: "e.g. Schedule A — 2027",
  schedule_key: "e.g. Schedule A",
  class_of_business: "e.g. Commercial Auto",
};

export default function BrokerContractNew() {
  const nav = useNavigate();

  const [me, setMe] = useState<{ id: number; name: string } | null>(null);
  const [programmes, setProgrammes] = useState<BrokerProgramme[]>([]);
  const [programId, setProgramId] = useState("");
  // Only the insurer↔broker spec matters here: a broker can only raise its own
  // contracts, and a reinsurance treaty is the carrier's to raise.
  const [spec, setSpec] = useState<ContractTypeSpec | null>(null);

  const [values, setValues] = useState<Record<string, string>>({});
  const [file, setFile] = useState<File | null>(null);

  const [touched, setTouched] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");

  useEffect(() => {
    getBrokerDashboard().then(d => setMe(d.broker)).catch(() => setMe(null));
    getBrokerProgrammes().then(setProgrammes).catch(() => setProgrammes([]));
    getContractTypes()
      .then(d => setSpec(d.types.find(t => t.key === "insurer_broker") ?? null))
      .catch(() => setMessage("Could not load the contract form."));
  }, []);

  const programme = useMemo(
    () => programmes.find(p => String(p.id) === programId) ?? null,
    [programmes, programId]);

  const termFields = (spec?.fields ?? []).filter(f => f.name !== "counterparty_party_id");
  const pick = (n: string) => termFields.find(f => f.name === n);
  const PLACED = ["name", "schedule_key", "inception_dt", "expiry_dt"];
  const otherRequired = termFields.filter(f => f.required && !PLACED.includes(f.name));

  function set(name: string, value: string) {
    setValues(v => ({ ...v, [name]: value }));
    setErrors(e => {
      if (!e[name]) return e;
      const { [name]: _drop, ...rest } = e;
      return rest;
    });
  }

  function missing(): boolean {
    if (!programId) return true;
    return termFields.some(f => f.required && !(values[f.name] ?? "").trim());
  }

  async function submit() {
    if (!spec) return;
    if (missing()) {
      setTouched(true);
      setMessage("Some required terms are missing — they are marked below.");
      return;
    }
    setMessage("");
    setErrors({});
    let created: { id: number } | null = null;
    try {
      setBusy("create");
      const body: Record<string, unknown> = {
        program_id: Number(programId),
        contract_type: "insurer_broker",
      };
      for (const f of termFields) {
        const raw = (values[f.name] ?? "").trim();
        if (!raw) continue;
        body[f.name] =
          f.kind === "int" ? Number.parseInt(raw, 10)
          : f.kind === "decimal" ? Number.parseFloat(raw)
          : raw;
      }
      created = await createContract(body as never);
    } catch (err) {
      const { message: m, errors: fe } = fieldErrors(err);
      setMessage(m);
      setErrors(fe);
      setBusy("");
      return;
    }

    // The contract exists from here on. A failure below must not read as "it
    // was not created" — it was, and its own page is where to finish.
    try {
      if (file) {
        setBusy("attach");
        await uploadDocument(created.id, file, { kind: "contract" });
      }
      setBusy("submit");
      await submitContract(created.id);
      nav(`/contracts/${created.id}`);
    } catch (err) {
      nav(`/contracts/${created.id}`, {
        state: { notice:
          `The contract was created, but ${busy === "attach"
            ? "the document could not be attached"
            : "it could not be submitted"} — `
          + `${fieldErrors(err).message} Finish from this page.` },
      });
    } finally {
      setBusy("");
    }
  }

  function input(f: ContractField | undefined) {
    if (!f) return null;
    const bad = errors[f.name] ?? (touched && f.required && !(values[f.name] ?? "").trim()
      ? `${f.label} is required.` : "");
    return (
      <div className="field" key={f.name}>
        <label>
          {f.label}
          {f.required
            ? <span style={{ color: "var(--p-crit-ink)" }}>*</span>
            : <span style={{ fontWeight: 500, color: "var(--p-faint)" }}> — optional</span>}
        </label>
        <input
          type={f.kind === "date" ? "date"
               : f.kind === "int" || f.kind === "decimal" ? "number" : "text"}
          step={f.kind === "decimal" ? "0.01" : undefined}
          placeholder={PLACEHOLDER[f.name]}
          value={values[f.name] ?? ""}
          onChange={e => set(f.name, e.target.value)}
          style={bad ? { borderColor: "var(--p-crit)" } : undefined}
        />
        <div className="hint" style={bad ? { color: "var(--p-crit-ink)" } : undefined}>
          {bad || f.hint}
        </div>
      </div>
    );
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Upload Contract</h2>
            <p>
              Upload a contract and assign it to yourself, under a programme a
              carrier has put you on.
            </p>
          </div>
          <div className="actions">
            <Link to="/broker/contracts" className="btn">
              <ArrowLeft size={14} /> My Contracts
            </Link>
            <button
              className="btn pri" type="button" disabled={!!busy || !spec}
              onClick={submit}
            >
              {busy
                ? <><Loader2 size={14} className="animate-spin" />{" "}
                    {busy === "create" ? "Creating…"
                     : busy === "attach" ? "Attaching…" : "Submitting…"}</>
                : <><Send size={14} /> Submit for approval</>}
            </button>
          </div>
        </div>

        {message && (
          <div className="note warn" style={{ marginBottom: 16, maxWidth: 700 }}>
            {message}
          </div>
        )}

        <div className="grid g-12">
          <div className="card pad">
            <div className="row2">
              <div className="field">
                <label>Carrier</label>
                <input className="ro" readOnly
                       value={programme?.carrier_name
                              ?? "Choose a programme first"} />
                <div className="hint">
                  The carrier that assigned you to the programme.
                </div>
              </div>
              <div className="field">
                <label>
                  Programme<span style={{ color: "var(--p-crit-ink)" }}>*</span>
                </label>
                <select
                  value={programId}
                  onChange={e => setProgramId(e.target.value)}
                  style={(touched && !programId)
                    ? { borderColor: "var(--p-crit)" } : undefined}
                >
                  <option value="">Select a programme</option>
                  {programmes.map(p => (
                    <option key={p.id} value={String(p.id)}>
                      {p.name}
                      {programmes.some(x => x.id !== p.id
                                            && x.carrier_id !== p.carrier_id)
                        ? ` — ${p.carrier_name}` : ""}
                    </option>
                  ))}
                </select>
                <div className="hint">
                  Only programmes you are assigned to. You do not add the
                  programme — the carrier does.
                </div>
              </div>
            </div>
            <div className="row2">
              <div className="field">
                <label>Broker</label>
                <input className="ro" readOnly
                       value={me ? `${me.name} (you)` : "you"} />
                <div className="hint">
                  A contract you upload is always assigned to you.
                </div>
              </div>
              {input(pick("name"))}
            </div>
            <div className="row2">
              {input(pick("inception_dt"))}
              {input(pick("expiry_dt"))}
            </div>
            {otherRequired.length > 0 && (
              <div className="row2">{otherRequired.map(input)}</div>
            )}

            <Dropzone
              file={file}
              onPick={setFile}
              disabled={!!busy}
              accept=".pdf,.doc,.docx"
              hint=".pdf, .doc, .docx"
              label={<><b>Drop the contract PDF here</b> or click to upload</>}
            />
            <div className="hint" style={{ marginTop: 8 }}>
              Attached with the submission so the carrier reviews the wording,
              not just a filename. Optional — the terms above stand on their own
              and the document can follow on the contract's page.
            </div>
          </div>

          <div className="card pad">
            <h3 style={{ margin: "0 0 12px", fontSize: 14 }}>
              What happens after you submit
            </h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 14,
                          fontSize: 12.5, color: "var(--p-muted)",
                          lineHeight: 1.55 }}>
              <div>
                <b style={{ color: "var(--p-ink)" }}>1 · Sent to the carrier</b><br />
                It appears in their approval queue, with the document you
                attached beside the terms.
              </div>
              <div>
                <b style={{ color: "var(--p-ink)" }}>2 · Pending</b><br />
                The contract is visible to you but{" "}
                <b>cannot be used for BDX Setup</b>.
              </div>
              <div>
                <b style={{ color: "var(--p-ink)" }}>3 · Approved or rejected</b><br />
                Approved makes it live. Rejected comes back with a reason so you
                can correct it and re-submit.
              </div>
              <div>
                <b style={{ color: "var(--p-ink)" }}>4 · Then the setup, then its own approval</b><br />
                Building the BDX templates on an approved contract is the next
                step — and that mapping is approved separately before it can
                process anything.
              </div>
            </div>
            <div className="note warn" style={{ marginTop: 16 }}>
              Nothing you upload becomes usable on your say-so. The carrier owns
              the book, so the carrier decides what enters it — the terms, and
              the mapping those terms are enforced through.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
