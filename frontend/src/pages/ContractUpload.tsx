/**
 * Upload an existing contract — the other way a contract gets here.
 *
 * The Raise flow builds a contract from its TERMS, for something being agreed
 * now. This is for one that already exists: a signed wording sitting in a
 * folder, which needs to be in Kavachio so its clauses can be read and its
 * rules generated. Asking someone to retype terms that are already written down
 * in the document they are holding is the wrong question.
 *
 * So the document IS the input here. Everything the Raise flow asks across
 * three steps — the name, the term, the class of business — is read out of the
 * wording by the same extraction that produces the clauses, and lands on the
 * contract record. What cannot be read from a document is asked for: which
 * programme it belongs to and which broker holds it, because a contract is
 * (programme × broker) and no wording states which row of that mesh it is.
 *
 * It runs the SAME upload the broker's own page runs — same endpoint, same
 * extraction, same halt when the wording defers to a document nobody supplied.
 * This is a second door into it, not a second implementation of it.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AlertTriangle, ArrowLeft, FileText, Loader2, Upload, X } from "lucide-react";
import { Dropzone } from "../components/Dropzone";
import { getHierarchy, type HierarchyProgramme } from "../api/hierarchy";
import {
  uploadContract, type ContractUploadCounts, type ExternalReference,
} from "../api/contracts";
import { getCounterparties, type Counterparty } from "../api/contractRecord";
import { currentMga, getTenantBrand } from "../auth";

export default function ContractUpload() {
  const nav = useNavigate();

  const [programmes, setProgrammes] = useState<HierarchyProgramme[]>([]);
  const [programId, setProgramId] = useState("");
  const [brokers, setBrokers] = useState<Counterparty[] | null>(null);
  const [brokerId, setBrokerId] = useState("");

  const [file, setFile] = useState<File | null>(null);
  const [refFiles, setRefFiles] = useState<File[]>([]);

  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState("");
  const [err, setErr] = useState("");
  // The pause: the wording defers to document(s) nobody supplied. Answered by
  // attaching them, or by going ahead without — in which case those clauses
  // produce no rule, which the result says plainly rather than implying
  // completeness.
  const [halt, setHalt] = useState<
    { refs: ExternalReference[]; resumeToken: string | null } | null>(null);
  const [haltFiles, setHaltFiles] = useState<File[]>([]);
  const [counts, setCounts] = useState<ContractUploadCounts | null>(null);

  useEffect(() => {
    getHierarchy().then(h => setProgrammes(h.programmes)).catch(() => setProgrammes([]));
  }, []);

  useEffect(() => {
    if (!programId) { setBrokers(null); return; }
    setBrokers(null);
    getCounterparties("broker", Number(programId))
      .then(setBrokers).catch(() => setBrokers([]));
  }, [programId]);

  const ready = !!programId && !!brokerId && !!file;
  // Shown, not asked for — you are signed in as the carrier. The design puts it
  // at the top of every contract screen because it is level 1 of the book.
  const carrierName = getTenantBrand()?.legal_name || currentMga();

  async function submit(opts?: { continueAnyway?: boolean; extraRefs?: File[] }) {
    if (!ready || !file) return;
    setBusy(true);
    setErr("");
    setStep("");
    try {
      const res = await uploadContract({
        programId: Number(programId),
        brokerPartyId: Number(brokerId),
        file,
        referenceFiles: [...refFiles, ...(opts?.extraRefs ?? [])],
        // Opted into, because this screen CAN answer the pause — and a wording
        // that defers to a document nobody has is exactly the case where
        // proceeding quietly loses rules.
        enableReferenceHalt: true,
        continueAnyway: opts?.continueAnyway,
        resumeToken: opts?.continueAnyway ? halt?.resumeToken ?? null : null,
        onProgress: setStep,
      });
      if ("halt" in res) { setHalt(res.halt); return; }
      setCounts(res.counts);
      nav(`/contracts/${res.cid}`, {
        state: {
          notice: res.deferred.length
            ? `Contract read, but it defers to ${res.deferred.join(", ")}, `
              + `which nobody supplied — those clauses produced no rule. `
              + `Attach the document below and re-read it.`
            : undefined,
        },
      });
    } catch (e) {
      const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      setErr(typeof d === "string" ? d
             : "That contract could not be read. Please try again.");
    } finally {
      setBusy(false);
      setStep("");
    }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Upload Contract</h2>
            <p>
              A wording you already have. Its terms are read out of the document
              rather than retyped.
            </p>
          </div>
          <div className="actions">
            <Link to="/contracts" className="btn">
              <ArrowLeft size={14} /> Contracts
            </Link>
            <button
              className="btn pri" type="button"
              disabled={!ready || busy || !!halt}
              onClick={() => submit()}
            >
              {busy
                ? <><Loader2 size={14} className="animate-spin" /> Reading…</>
                : <><FileText size={14} /> Upload and read</>}
            </button>
          </div>
        </div>

        {err && (
          <div className="note warn" style={{ marginBottom: 16, maxWidth: 620 }}>
            {err}
          </div>
        )}

        <div className="grid g-12">
          <div className="card pad">
            <div className="grid g-3">
              <div className="field">
                <label>Carrier</label>
                <input className="ro" value={carrierName} readOnly />
                <div className="hint">You. Level 1 of the book.</div>
              </div>
              <div className="field">
                <label>Programme</label>
                <select
                  value={programId}
                  onChange={e => { setProgramId(e.target.value); setBrokerId(""); }}
                >
                  <option value="">Select a programme</option>
                  {programmes.map(p => (
                    <option key={p.id} value={String(p.id)}>{p.name}</option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>Broker</label>
                <select
                  value={brokerId} disabled={!programId}
                  onChange={e => setBrokerId(e.target.value)}
                >
                  <option value="">
                    {programId ? "Select a broker…" : "Choose a programme first"}
                  </option>
                  {(brokers ?? []).map(b => (
                    <option key={b.id} value={String(b.id)}>{b.name}</option>
                  ))}
                </select>
                {programId && brokers?.length === 0 ? (
                  <div className="hint" style={{ color: "var(--p-warn-ink)" }}>
                    No brokers are on this programme. Put one on it from the{" "}
                    <Link to="/contracts/new" className="linkish">Raise flow</Link>{" "}
                    or the programme's own screen.
                  </div>
                ) : (
                  <div className="hint">
                    Only brokers assigned to the chosen programme.
                  </div>
                )}
              </div>
            </div>

            <div className="divider" />

            <Dropzone
              file={file}
              onPick={f => { setFile(f); setHalt(null); }}
              disabled={busy}
              accept=".pdf,.doc,.docx"
              hint=".pdf, .doc, .docx"
              label={<><b>Drop the contract PDF here</b> or click to upload</>}
            />
            <div className="hint" style={{ marginTop: 8 }}>
              Its clauses are read on upload, and its name, term and class of
              business are taken from the document.
            </div>

            <div className="divider" />

            <label className="btn" style={{ cursor: "pointer" }}>
              <Upload size={13} /> Add a reference document
              <input
                type="file" style={{ display: "none" }} disabled={busy}
                onChange={e => {
                  const f = e.target.files?.[0];
                  if (f) setRefFiles(r => [...r, f]);
                  e.target.value = "";
                }}
              />
            </label>
            <div className="hint" style={{ marginTop: 8 }}>
              Optional up front. If the wording defers to a document you have not
              supplied, the read pauses and asks for it — the clauses pointing at
              it cannot become rules without it.
            </div>
            {refFiles.length > 0 && (
              <div style={{ marginTop: 10 }}>
                {refFiles.map((f, i) => (
                  <div className="kv" key={i}>
                    <span className="k">{f.name}</span>
                    <span
                      className="linkish" role="button"
                      onClick={() => setRefFiles(r => r.filter((_, j) => j !== i))}
                    >
                      <X size={12} /> Remove
                    </span>
                  </div>
                ))}
              </div>
            )}

            {busy && step && (
              <div className="note" style={{ marginTop: 14 }}>{step}</div>
            )}
            {counts && (
              <div className="note ok" style={{ marginTop: 14 }}>
                {counts.clauses} clauses, {counts.rules} rules.
              </div>
            )}
          </div>

          <div className="card pad">
            <h3 style={{ margin: "0 0 12px", fontSize: 14 }}>What happens next</h3>
            <div className="steps" style={{ gridTemplateColumns: "1fr", gap: 14 }}>
              <div className="step s1">
                <span className="n">1</span>
                <div>
                  <h4>Groups under the broker</h4>
                  <p>It joins that broker's contracts on this programme.</p>
                </div>
              </div>
              <div className="step s2">
                <span className="n">2</span>
                <div>
                  <h4>Clauses extracted</h4>
                  <p>Caps, minimums and required fields are read from the document.</p>
                </div>
              </div>
              <div className="step s3">
                <span className="n">3</span>
                <div>
                  <h4>Rules generated</h4>
                  <p>Each clause becomes a validation rule on an output column.</p>
                </div>
              </div>
            </div>

            <div className="divider" />
            <div className="note">
              <b>Which one do I want?</b> This screen is for a wording you already
              have — its terms are read out of it.{" "}
              <Link to="/contracts/new" className="linkish">Create Contract</Link>{" "}
              is for terms being agreed now, with no signed document yet. Both end
              at the same record.
            </div>
            <div className="note" style={{ marginTop: 10 }}>
              <b>This can take a while.</b> A long contract is read section by
              section and then clause by clause — 75+ model calls is normal, and
              several minutes with it. Leave the page open; if the connection
              drops the read carries on and the contract is picked up when it
              lands.
            </div>
          </div>
        </div>

        {/* The pause. Not an error — the contract is fine, it just points at
            something nobody has handed over yet. */}
        {halt && (
          <div className="card" style={{ marginTop: 18 }}>
            <div className="card-h">
              <AlertTriangle size={16} className="ci" />
              <h3>This contract refers to another document</h3>
            </div>
            <div style={{ padding: "16px 20px" }}>
              <p style={{ margin: 0, fontSize: 13 }}>
                The wording defers part of its content to{" "}
                <b>
                  {halt.refs.map(r => r.document_name).filter(Boolean).join(", ")
                   || "a document it does not name clearly"}
                </b>. Until that is supplied, the clauses pointing at it cannot
                become rules.
              </p>
              <div className="rowacts" style={{ marginTop: 14 }}>
                <label className="btn" style={{ cursor: "pointer" }}>
                  <Upload size={13} /> Attach it
                  <input
                    type="file" style={{ display: "none" }} disabled={busy}
                    onChange={e => {
                      const f = e.target.files?.[0];
                      if (f) setHaltFiles(r => [...r, f]);
                      e.target.value = "";
                    }}
                  />
                </label>
                {haltFiles.length > 0 && (
                  <span className="sub">{haltFiles.map(f => f.name).join(", ")}</span>
                )}
                <button
                  className="btn pri" type="button"
                  disabled={busy || haltFiles.length === 0}
                  onClick={() => submit({ extraRefs: haltFiles })}
                >
                  Read it again with that
                </button>
                <button
                  className="btn" type="button" disabled={busy}
                  onClick={() => submit({ continueAnyway: true })}
                >
                  Continue without it
                </button>
              </div>
              <div className="hint" style={{ marginTop: 10 }}>
                Continuing is a real choice, not a shortcut: the contract is
                saved, and the clauses that needed that document simply produce
                nothing. You can attach it later and re-read the contract.
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
