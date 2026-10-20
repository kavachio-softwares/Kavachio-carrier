/**
 * Upload a treaty that was already signed — on paper, outside Kavachio.
 *
 * The signed document already says everything: the term, the type, the
 * layers, who holds what. So the carrier gives only what the document cannot
 * know — which of THEIR programmes it is for — and the file. Kavachio reads it
 * (POST /treaties/read) and fills the form; the carrier checks what was read,
 * adds any reinsurer not yet on file, and confirms. Saving puts it in force
 * (or sends it to the carrier admin, for a carrier user).
 *
 * A file with no readable text, or a reading that fails, still works: the
 * same form is offered empty, to fill in by hand.
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { FileText, Loader2, Plus, Sparkles } from "lucide-react";
import Modal from "../ui/Modal";
import { FileDrop } from "../ui/FileDrop";
import { currentMga } from "../../auth";
import { getCounterparties, fieldErrors } from "../../api/contractRecord";
import { getHierarchy } from "../../api/hierarchy";
import {
  createReinsurer, getTreatyTypes, readSignedTreaty, uploadSignedTreaty,
  type TreatyInput, type TreatyReading, type TreatyTypeSpec,
} from "../../api/treaty";
import { EMPTY_TREATY, TreatyEditor, treatyForSave } from "./TreatyEditor";

type Stage = "file" | "reading" | "check";

export function UploadTreatyModal({ open, onClose, programId: initialProgramId, switcher }: {
  /** The Contracts list's broker / reinsurance choice, at the top. */
  switcher?: React.ReactNode;
  open: boolean;
  onClose: () => void;
  programId?: number;
}) {
  const nav = useNavigate();
  const [stage, setStage] = useState<Stage>("file");
  const [types, setTypes] = useState<TreatyTypeSpec[] | null>(null);
  const [reinsurers, setReinsurers] = useState<{ id: number; name: string }[]>([]);
  const [programmes, setProgrammes] = useState<{ id: number; name: string }[]>([]);
  const [programId, setProgramId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [reading, setReading] = useState<TreatyReading | null>(null);
  const [name, setName] = useState("");
  const [inception, setInception] = useState("");
  const [expiry, setExpiry] = useState("");
  const [yoa, setYoa] = useState("");
  const [notice, setNotice] = useState("");
  const [executed, setExecuted] = useState("");
  const [treaty, setTreaty] = useState<TreatyInput>(EMPTY_TREATY);
  const [extra, setExtra] = useState<number[]>([]);
  const [unmatched, setUnmatched] = useState<string[]>([]);
  const [adding, setAdding] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [tried, setTried] = useState(false);
  // What was read from a SIGNED document is shown as it was read, not as a
  // form: its shares and terms are what the reinsurers signed. Unlocked only
  // when somebody says the reading got something wrong — and from the start
  // when there was nothing read (filled in by hand, or the reading failed).
  const [locked, setLocked] = useState(false);

  useEffect(() => {
    if (!open) return;
    setStage("file");
    setProgramId(initialProgramId ? String(initialProgramId) : "");
    setFile(null); setReading(null);
    setName(""); setInception(""); setExpiry(""); setYoa(""); setNotice("");
    setExecuted(""); setTreaty(EMPTY_TREATY); setExtra([]); setUnmatched([]);
    setErrors({}); setErr(""); setNote(""); setTried(false); setLocked(false);
    getTreatyTypes().then(setTypes).catch(() => setTypes([]));
    getCounterparties("reinsurer").then(l => setReinsurers(
      l.map(c => ({ id: c.id, name: c.name })))).catch(() => {});
    getHierarchy().then(h => {
      const list = h.programmes.map(p => ({ id: p.id, name: p.name }));
      setProgrammes(list);
      // One programme: nothing to choose.
      if (!initialProgramId && list.length === 1) setProgramId(String(list[0].id));
    }).catch(() => {});
  }, [open, initialProgramId]);

  /** Read the document, and fill the form with what it says. */
  async function read() {
    if (!file) return;
    setStage("reading"); setErr(""); setNote("");
    try {
      const r = await readSignedTreaty(file);
      setReading(r);
      setName(r.name ?? "");
      setInception(r.inception_dt ?? "");
      setExpiry(r.expiry_dt ?? "");
      setYoa(r.year_of_account ?? (r.inception_dt ?? "").slice(0, 4));
      setNotice(r.notice_period_days != null ? String(r.notice_period_days) : "");
      setExecuted(r.executed_date ?? "");
      setTreaty(r.treaty ?? EMPTY_TREATY);
      setUnmatched(r.unmatched_reinsurers);
      // Lock what was read, unless something essential is missing from it.
      setLocked(!!r.treaty && !!r.name && !!r.inception_dt && !!r.expiry_dt);
      setNote(r.treaty
        ? "Read from the signed treaty. These are the signed terms, so they are "
          + "shown as read — check them against the document."
        : "The details were read, but not the treaty type — choose it below.");
    } catch (e) {
      setErr(`${fieldErrors(e).message} You can fill it in below instead.`);
    } finally {
      setStage("check");
    }
  }

  /** Add a reinsurer the document names but the carrier does not have yet,
   *  and put it on every line that named it. */
  async function addReinsurer(written: string) {
    setAdding(written); setErr("");
    try {
      const r = await createReinsurer(currentMga(), { legal_name: written });
      setReinsurers(l => [...l, { id: r.id, name: r.legal_name }]);
      setTreaty(t => ({ ...t, layers: t.layers.map(lay => ({
        ...lay, shares: lay.shares.map(sh =>
          sh.reinsurer_party_id == null && sh.reinsurer_name === written
            ? { ...sh, reinsurer_party_id: r.id } : sh) })) }));
      setUnmatched(u => u.filter(x => x !== written));
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setAdding("");
    }
  }

  const lead = treaty.layers[0]?.shares.find(s => s.is_lead)?.reinsurer_party_id ?? null;
  const missing = useMemo(() => {
    const out: string[] = [];
    if (!programId) out.push("the programme");
    if (!name.trim()) out.push("a name");
    if (!inception || !expiry) out.push("the term");
    if (!yoa.trim()) out.push("the year of account");
    if (!notice.trim()) out.push("the notice period");
    if (!treaty.treaty_type) out.push("the treaty type");
    if (unmatched.length) out.push("the reinsurers not on file yet");
    if (lead == null) out.push("a starred lead reinsurer");
    return out;
  }, [programId, name, inception, expiry, yoa, notice, treaty.treaty_type,
      unmatched.length, lead]);

  async function save() {
    setTried(true);
    if (missing.length) { setErr(`Still needed: ${missing.join(", ")}.`); return; }
    setBusy(true); setErr(""); setErrors({});
    try {
      const rec = await uploadSignedTreaty({
        file: file!, program_id: Number(programId), name: name.trim(),
        inception_dt: inception, expiry_dt: expiry, year_of_account: yoa.trim(),
        notice_period_days: Number(notice), executed_date: executed || null,
        treaty: treatyForSave(treaty), program_ids: extra, reading,
      });
      onClose();
      nav(`/contracts/${rec.id}`);
    } catch (e) {
      const fe = fieldErrors(e);
      setErr(fe.message);
      setErrors(fe.errors as Record<string, string>);
    } finally {
      setBusy(false);
    }
  }

  const bad = (v: string) => tried && !v.trim()
    ? { borderColor: "var(--p-crit)" } : undefined;
  const req = <span style={{ color: "var(--p-crit-ink)" }}>*</span>;

  const footer = stage === "check" ? (
    <>
      <button className="btn" type="button" style={{ marginRight: "auto" }}
              onClick={() => setStage("file")}>Back</button>
      <button className="btn" type="button" onClick={onClose}>Cancel</button>
      <button className="btn pri" type="button" disabled={busy} onClick={save}>
        {busy ? "Uploading…" : "Upload and put in force"}
      </button>
    </>
  ) : (
    <>
      <button className="btn" type="button" style={{ marginRight: "auto" }}
              disabled={stage === "reading" || !file || !programId}
              title={!file ? "Attach the signed treaty first" : undefined}
              onClick={() => setStage("check")}>
        Fill it in by hand
      </button>
      <button className="btn" type="button" onClick={onClose}>Cancel</button>
      <button className="btn pri" type="button"
              disabled={!file || !programId || stage === "reading"} onClick={read}>
        {stage === "reading"
          ? <><Loader2 size={14} className="animate-spin" /> Reading…</>
          : <><Sparkles size={14} /> Read the treaty</>}
      </button>
    </>
  );

  return (
    <Modal open={open} onClose={onClose}
           size={stage === "check" ? "4xl" : "lg"}
           title="Upload a signed treaty" footer={footer}>
      <div className="proto proto-embed">
        {stage === "file" && switcher}
        {stage !== "check" ? (
          <>
            <p className="sub" style={{ marginTop: 0 }}>
              Upload the treaty the reinsurers signed. Kavachio reads its terms,
              its layers and who holds what — you check them, and it goes in
              force.
            </p>
            <div className="field">
              <label>Programme it is for{req}</label>
              <select value={programId} onChange={e => setProgramId(e.target.value)}
                      disabled={stage === "reading"}>
                <option value="">Choose…</option>
                {programmes.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </div>
            <FileDrop label="Signed treaty" required tone="required"
                      icon={<FileText size={15} />} accept=".pdf,.docx"
                      file={file} onPick={setFile} disabled={stage === "reading"}
                      hint="The signed slip or wording — PDF or Word" />
            {stage === "reading" && (
              <div className="note" style={{ marginTop: 14 }}>
                Reading the treaty — this usually takes 15 to 30 seconds.
              </div>
            )}
          </>
        ) : (
          <>
            {note && <div className="note ok" style={{ marginBottom: 12 }}>{note}</div>}
            {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}

            {unmatched.length > 0 && (
              <div className="note warn" style={{ marginBottom: 14 }}>
                <b>Not on file yet:</b> the treaty names{" "}
                {unmatched.length === 1 ? "a reinsurer" : "reinsurers"} you have not
                added. Add {unmatched.length === 1 ? "it" : "them"} to put
                {unmatched.length === 1 ? " its" : " their"} lines on the panel.
                <div className="rowacts">
                  {unmatched.map(u => (
                    <button key={u} type="button" className="btn sm"
                            disabled={!!adding} onClick={() => addReinsurer(u)}>
                      {adding === u ? <Loader2 size={12} className="animate-spin" />
                                    : <Plus size={12} />} Add {u}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div className="grid g-3" style={{ gap: 14 }}>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Programme{req}</label>
                <select value={programId} onChange={e => setProgramId(e.target.value)}
                        style={bad(programId)}>
                  <option value="">Choose…</option>
                  {programmes.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </div>
              <div className="field" style={{ marginBottom: 0, gridColumn: "span 2" }}>
                <label>Treaty name{req}</label>
                <input value={name} readOnly={locked} className={locked ? "ro" : undefined} onChange={e => setName(e.target.value)} style={bad(name)} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Inception{req}</label>
                <input type="date" value={inception} readOnly={locked} className={locked ? "ro" : undefined}
                       onChange={e => setInception(e.target.value)} style={bad(inception)} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Expiry{req}</label>
                <input type="date" value={expiry} readOnly={locked} className={locked ? "ro" : undefined}
                       onChange={e => setExpiry(e.target.value)} style={bad(expiry)} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Executed on</label>
                <input type="date" value={executed} readOnly={locked} className={locked ? "ro" : undefined} onChange={e => setExecuted(e.target.value)} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Year of account{req}</label>
                <input value={yoa} readOnly={locked} className={locked ? "ro" : undefined} onChange={e => setYoa(e.target.value)} style={bad(yoa)} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Notice period (days){req}</label>
                <input type="number" min={0} value={notice} readOnly={locked} className={locked ? "ro" : undefined}
                       onChange={e => setNotice(e.target.value)} style={bad(notice)} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Signed copy</label>
                <input className="ro" readOnly value={file?.name ?? "—"} />
              </div>
            </div>

            <div className="fh" style={{ marginTop: 18, display: "flex",
                                         alignItems: "center", gap: 10 }}>
              Treaty structure
              {locked && (
                <button type="button" className="btn sm ghost" style={{ marginLeft: "auto" }}
                        onClick={() => setLocked(false)}>
                  Something read wrong? Correct it
                </button>
              )}
            </div>
            <TreatyEditor
              disabled={locked}
              value={treaty} onChange={v => { setTreaty(v); setErrors({}); }}
              types={types} reinsurers={reinsurers} errors={errors} showErrors={tried}
              onReinsurerAdded={r => setReinsurers(l => [...l, r])}
              programmes={{ all: programmes, selected: extra,
                            homeId: programId ? Number(programId) : null,
                            onChange: setExtra }} />
          </>
        )}
      </div>
    </Modal>
  );
}
