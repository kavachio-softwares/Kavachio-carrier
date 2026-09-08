/**
 * Create a Contract — the four-step flow from the carrier design
 * (kavachio_carrier_centric_design.html: c-generate → c-draft → c-review →
 * c-sign).
 *
 *   1 Terms            what you are making, who it is with, and the limits you
 *                      agreed — each limit carrying what happens when a file
 *                      breaks it
 *   2 Wording          the sections, written from those terms, editable
 *   3 Read it through  the finished document, the checks it will run, and
 *                      anything worth a second look
 *   4 Signatures       who signs — handed to the signature screen
 *
 * THE IDEA THE FLOW IS BUILT ON. A limit typed in step 1 becomes two things at
 * once: a sentence in the contract and a check on every bordereau row. They
 * stay tied because the sentence stores a TOKEN, not the number — change the
 * cap and the wording and the check both move. The design puts it plainly:
 * type "15%" by hand and the two quietly drift apart, which is how a contract
 * ends up saying one thing while the system checks another.
 *
 * Which is also why this flow produces better contracts than uploading one: a
 * contract written from its own terms never has a clause the checks cannot
 * read. Somebody who already HAS a signed PDF should not be here — the footer
 * points them at Upload Contract, which reads what it can out of the document.
 *
 * Nothing is written until the last step. A programme or broker created inline
 * is the exception, and says so: those are shared things.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, Download, FileText, Plus, Trash2 } from "lucide-react";
import { getHierarchy, type HierarchyProgramme } from "../api/hierarchy";
import { WordingEditor } from "../components/WordingEditor";
import { currentMga, getTenantBrand } from "../auth";
import {
  createContract, createEndorsement, downloadDraft, fieldErrors, getContract,
  getContractTypes, getCounterparties, listContracts, previewEndorsement,
  previewWording,
  type AgreedLimits, type AgreedLimitSpec, type ContractTypeSpec,
  type ContractRecord as ContractRecordT, type Counterparty,
  type ContractField, type EndorsementPreview, type FieldErrors,
  type LimitGroup, type Signer,
  type WordingPreview, type WordingSection,
} from "../api/contractRecord";

/** Examples, only where one actually helps. A date input needs none, and a
 *  placeholder on every field is noise. */
const PLACEHOLDER: Record<string, string> = {
  name: "e.g. Schedule A — 2027",
  schedule_key: "e.g. SCH-A",
  class_of_business: "e.g. Commercial Property",
  year_of_account: "e.g. 2027",
  notice_period_days: "e.g. 60",
};

const STEPS = [
  { key: "terms", label: "Terms" },
  { key: "wording", label: "Wording" },
  { key: "review", label: "Read it through" },
  { key: "signatures", label: "Signatures" },
] as const;

/** What you are making. The design asks this first because the three end in
 *  different places — and they really do, so each option has to DO something
 *  different or it should not be offered:
 *
 *    new       everything starts blank
 *    renewal   pick last year's contract; its terms and wording come across so
 *              the broker only reads the numbers that moved, and the new
 *              contract points back at it
 *    endorse   belongs on the contract that is already running, not here — so
 *              this option routes there instead of pretending to handle it
 */
const KINDS = [
  { key: "new", title: "A brand-new contract",
    sub: "Nothing existed before. Everything below starts blank." },
  { key: "renew", title: "A renewal",
    sub: "Next year's version of one you already have. Its terms come across and the two stay linked." },
  { key: "endorse", title: "A mid-term change",
    sub: "An endorsement — the contract keeps running; only what you change is re-agreed." },
] as const;

export default function ContractNew() {
  const nav = useNavigate();
  const [params] = useSearchParams();

  const [step, setStep] = useState(0);
  const [furthest, setFurthest] = useState(0);
  const [kind, setKind] = useState<string>("new");

  const [specs, setSpecs] = useState<ContractTypeSpec[] | null>(null);
  const [limitSpec, setLimitSpec] = useState<AgreedLimitSpec[]>([]);
  const [limitGroups, setLimitGroups] = useState<LimitGroup[]>([]);
  const [typeKey, setTypeKey] = useState("");

  const [programmes, setProgrammes] = useState<HierarchyProgramme[]>([]);
  const [programId, setProgramId] = useState(params.get("program_id") ?? "");
  const [counterparties, setCounterparties] = useState<Counterparty[] | null>(null);
  const [brokerId, setBrokerId] = useState("");

  const [values, setValues] = useState<Record<string, string>>({});
  const [limits, setLimits] = useState<AgreedLimits>({});

  const [sections, setSections] = useState<WordingSection[] | null>(null);
  const [activeSection, setActiveSection] = useState(0);
  // Bumped when the wording is REGENERATED, so the editor rebuilds its DOM.
  // Ordinary typing must not trigger that — rebuilding on every keystroke
  // throws the caret to the start of the line.
  const [wordingVersion, setWordingVersion] = useState(0);
  const insertChip = useRef<((token: string) => void) | null>(null);
  const [preview, setPreview] = useState<WordingPreview | null>(null);

  // Who signs. Named here only to save doing it later — Kavachio sends
  // nothing, which step 4 says plainly.
  const [signers, setSigners] = useState<Signer[]>([]);
  const [sgSide, setSgSide] = useState<"carrier" | "counterparty">("carrier");
  const [sgName, setSgName] = useState("");
  const [sgEmail, setSgEmail] = useState("");
  const [sgRole, setSgRole] = useState("");

  // Endorsement: which live contract is being changed, and what the change
  // looks like. Kept apart from the create state because an endorsement does
  // not create a contract — it amends one.
  const [endorseId, setEndorseId] = useState("");
  const [endorsable, setEndorsable] = useState<ContractRecordT[]>([]);
  const [endorseFrom, setEndorseFrom] = useState("");
  const [endorseNote, setEndorseNote] = useState("");
  const [endorsePv, setEndorsePv] = useState<EndorsementPreview | null>(null);

  // Renewal: which contract this one renews, and the pool to pick from.
  const [renewsId, setRenewsId] = useState("");
  const [renewable, setRenewable] = useState<ContractRecordT[]>([]);

  const [touched, setTouched] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");

  const carrierName = getTenantBrand()?.legal_name || currentMga();

  /** What a chip is called in the toolbar and in its tooltip. The limits use
   *  the plain-English question from step 1; the handful that are not limits
   *  (the parties, the term) are named here. */
  const tokenLabels = useMemo(() => {
    const out: Record<string, string> = {
      contract_name: "Contract name", inception: "Start date",
      expiry: "End date", class_of_business: "Class of business",
      carrier_name: "Carrier", counterparty_name: "Broker",
      programme_name: "Programme", notice_period_days: "Notice to cancel",
    };
    limitSpec.forEach(l => { out[l.name] = l.question; });
    return out;
  }, [limitSpec]);

  useEffect(() => {
    getContractTypes()
      .then(d => {
        setSpecs(d.types);
        setLimitSpec(d.agreed_limits);
        setLimitGroups(d.limit_groups);
        setTypeKey(k => k || d.default);
      })
      .catch(() => setMessage("Could not load the contract form."));
    getHierarchy().then(h => setProgrammes(h.programmes)).catch(() => setProgrammes([]));
    // Anything in force or lapsed can be renewed. A draft cannot — there is
    // nothing agreed yet to carry forward.
    listContracts()
      .then(rows => {
        setRenewable(rows.filter(
          c => ["active", "expired", "terminated"].includes(c.lifecycle)));
        // Only a contract that is RUNNING can be endorsed. A draft is changed
        // by editing its terms; one that has ended is not changed at all.
        setEndorsable(rows.filter(c => ["active", "expired"].includes(c.lifecycle)));
      })
      .catch(() => { setRenewable([]); setEndorsable([]); });
  }, []);

  /** Load the contract being endorsed, and start from its CURRENT terms — the
   *  change is expressed by editing them, which is how somebody actually
   *  thinks about a mid-term change. */
  async function loadEndorseSource(id: string) {
    setEndorseId(id);
    setEndorsePv(null);
    if (!id) return;
    setBusy("endorse-load");
    try {
      const prior = await getContract(Number(id));
      setLimits(prior.agreed_limits ?? {});
      setValues(v => ({ ...v, name: prior.name }));
      setProgramId(prior.programme ? String(prior.programme.id) : "");
      setBrokerId(prior.counterparty ? String(prior.counterparty.id) : "");
    } catch (e) {
      setMessage(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  /** What the change would say, and which checks would move. */
  const refreshEndorsement = useCallback(async () => {
    if (!endorseId) return;
    try {
      setEndorsePv(await previewEndorsement(Number(endorseId), {
        agreed_limits: limits, effective_from: endorseFrom || null,
        note: endorseNote || null }));
    } catch (e) { setMessage(fieldErrors(e).message); }
  }, [endorseId, limits, endorseFrom, endorseNote]);

  useEffect(() => {
    if (kind !== "endorse" || !endorseId) return;
    const t = setTimeout(refreshEndorsement, 350);
    return () => clearTimeout(t);
  }, [kind, endorseId, refreshEndorsement]);

  async function endorse() {
    if (!endorseId) return;
    setBusy("endorse");
    setMessage("");
    try {
      const r = await createEndorsement(Number(endorseId), {
        agreed_limits: limits, effective_from: endorseFrom || null,
        note: endorseNote || null, sections: endorsePv?.sections });
      nav(`/contracts/${r.id}`, { state: { notice:
        `Endorsement ${r.endorsement.number} attached. The wording and the `
        + `endorsement are both active — re-read the contract to rebuild its `
        + `rules from the pair.` } });
    } catch (e) {
      setMessage(fieldErrors(e).message);
    } finally { setBusy(""); }
  }

  /** Bring last year's contract across. Everything the new one starts from,
   *  except the term — a renewal is a new year, so the dates are the one thing
   *  that must be stated fresh. */
  async function loadRenewalSource(id: string) {
    setRenewsId(id);
    if (!id) return;
    setBusy("renewal");
    try {
      const prior = await getContract(Number(id));
      setProgramId(prior.programme ? String(prior.programme.id) : "");
      setBrokerId(prior.counterparty ? String(prior.counterparty.id) : "");
      setValues(v => ({
        ...v,
        name: prior.name ? `${prior.name} — renewal` : v.name,
        schedule_key: prior.schedule_key ?? v.schedule_key ?? "",
        class_of_business: prior.class_of_business ?? v.class_of_business ?? "",
        notice_period_days: prior.notice_period_days != null
          ? String(prior.notice_period_days) : (v.notice_period_days ?? ""),
      }));
      if (prior.agreed_limits) setLimits(prior.agreed_limits);
      // Its wording too, so the broker reads the same document with new numbers.
      if (prior.wording_sections?.length) {
        setSections(prior.wording_sections);
        setWordingVersion(v => v + 1);
      }
    } catch (e) {
      setMessage(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  const spec = useMemo(
    () => (specs ?? []).find(t => t.key === typeKey) ?? null, [specs, typeKey]);

  useEffect(() => {
    if (!programId) { setCounterparties(null); return; }
    setCounterparties(null);
    getCounterparties("broker", Number(programId))
      .then(setCounterparties).catch(() => setCounterparties([]));
  }, [programId]);

  const programme = programmes.find(p => String(p.id) === programId);
  const counterparty = (counterparties ?? []).find(c => String(c.id) === brokerId);

  // The contract's own identity — everything the chosen TYPE asks for that is
  // not one of the agreed limits. Taken from the server's spec rather than
  // written out here, so "required" on this form and "required" on the server
  // are the same list. Hardcoding it is what let the form call class of
  // business optional while the server refused to save without it.
  const limitNames = useMemo(
    () => new Set(limitSpec.map(l => l.name)), [limitSpec]);
  const basicFields = (spec?.fields ?? []).filter(
    f => f.name !== "counterparty_party_id" && !limitNames.has(f.name));
  const optionalBasics = basicFields.filter(f => !f.required);
  const filledOptional = optionalBasics.filter(
    f => (values[f.name] ?? "").trim()).length;

  // A folded field that is filled in, or that the server has just objected to,
  // is a field the user needs to see — leaving it hidden would show a marked
  // input nobody can find.
  useEffect(() => {
    if (showMoreBasics) return;
    const hidden = optionalBasics.some(
      f => (values[f.name] ?? "").trim() || errors[f.name]);
    if (hidden) setShowMoreBasics(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values, errors, optionalBasics.length]);

  /** One identity field. Placeholder and hint come from the spec too. */
  function input(f: ContractField) {
    const bad = errors[f.name]
      ?? (touched && f.required && !(values[f.name] ?? "").trim()
          ? `${f.label} is required.` : "");
    return (
      <div className="field" key={f.name} style={{ marginBottom: 0 }}>
        <label>
          {f.label}
          {f.required
            ? <span style={{ color: "var(--p-crit-ink)" }}>*</span>
            : <span className="muted" style={{ fontWeight: 500 }}> — optional</span>}
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

  /** What the preview and the composer both need. One shape, so the wording,
   *  the checks and the PDF can never be computed from different inputs. */
  const wordingInput = useCallback((secs?: WordingSection[] | null) => ({
    contract_type: typeKey,
    values: {
      name: values.name, inception_dt: values.inception_dt,
      expiry_dt: values.expiry_dt, class_of_business: values.class_of_business,
      schedule_key: values.schedule_key,
      notice_period_days: values.notice_period_days,
    },
    agreed_limits: limits,
    sections: secs ?? undefined,
    carrier_name: carrierName,
    counterparty_name: counterparty?.name ?? null,
    programme_name: programme?.name ?? null,
  }), [typeKey, values, limits, carrierName, counterparty, programme]);

  /** Re-read the wording. Called when entering steps 2 and 3, and whenever a
   *  term changes while they are open — the chips have to follow. */
  const refresh = useCallback(async (secs?: WordingSection[] | null) => {
    try {
      const pv = await previewWording(wordingInput(secs));
      setPreview(pv);
      setSections(pv.sections);
      if (!secs) setWordingVersion(v => v + 1);
      return pv;
    } catch (e) {
      setMessage(fieldErrors(e).message);
      return null;
    }
  }, [wordingInput]);

  // Keep the preview current while step 1 is being filled in, so the check
  // count and the per-row "what this becomes" are the real answers rather than
  // a guess the screen makes for itself. Debounced and cheap — the preview
  // involves no model call, only arithmetic on the terms.
  useEffect(() => {
    if (step !== 0) return;
    const t = setTimeout(() => {
      previewWording(wordingInput(null)).then(setPreview).catch(() => {});
    }, 350);
    return () => clearTimeout(t);
  }, [step, wordingInput]);

  function set(name: string, value: string) {
    setValues(v => ({ ...v, [name]: value }));
    setErrors(e => {
      if (!e[name]) return e;
      const { [name]: _drop, ...rest } = e;
      return rest;
    });
  }

  function setLimit(key: string, patch: Partial<AgreedLimits[string]>) {
    setLimits(l => {
      const cur = l[key] ?? { value: "" };
      const next = { ...cur, ...patch };
      if (next.value === "") { const { [key]: _d, ...rest } = l; return rest; }
      return { ...l, [key]: next };
    });
  }

  /** Everything still missing, checked against the SERVER's own required list.
   *  The server re-checks on save and is the authority; this exists so the gap
   *  is named on the step that owns it rather than surfacing as a refusal three
   *  steps later with nothing marked. */
  function missing(): string[] {
    const out: string[] = [];
    if (!programId) out.push("Choose the programme this contract sits on.");
    if (!brokerId && spec) {
      out.push(`Choose the ${spec.counterparty_label.toLowerCase()} this `
               + `contract is with.`);
    }
    for (const f of basicFields) {
      if (f.required && !(values[f.name] ?? "").trim()) {
        out.push(`${f.label} is required.`);
      }
    }
    return out;
  }

  async function go(to: number) {
    if (to > step) {
      const gaps = missing();
      if (gaps.length) {
        setTouched(true);
        setMessage(gaps[0]);
        return;
      }
    }
    setMessage("");
    // Entering the wording or the review re-reads from the current terms, so a
    // number changed on step 1 is reflected in both.
    if (to >= 1) await refresh(to === 1 ? sections : sections);
    setStep(to);
    setFurthest(f => Math.max(f, to));
  }

  async function create(mode: "draft" | "review" | "live") {
    if (!spec) return;
    setBusy("create");
    setErrors({});
    setMessage("");
    try {
      const created = await createContract({
        program_id: Number(programId),
        contract_type: typeKey,
        counterparty_party_id: Number(brokerId),
        name: values.name,
        schedule_key: values.schedule_key || null,
        class_of_business: values.class_of_business || null,
        inception_dt: values.inception_dt,
        expiry_dt: values.expiry_dt,
        notice_period_days: values.notice_period_days
          ? Number(values.notice_period_days) : null,
        agreed_limits: limits,
        renews_contract_id: kind === "renew" && renewsId ? Number(renewsId) : null,
        signers,
        wording_sections: (sections ?? []).map(
          ({ rendered: _r, tokens: _t, ...keep }) => keep),
        create_as: mode,
      } as never);
      nav(`/contracts/${created.id}`);
    } catch (err) {
      const { message: m, errors: fe } = fieldErrors(err);
      // Name the fields in the message. The server's own sentence says only
      // that something is missing, which is no use on step 4 where none of
      // those inputs are on screen.
      const named = Object.keys(fe)
        .map(k => basicFields.find(f => f.name === k)?.label ?? k);
      setMessage(named.length
        ? `${m} Missing: ${named.join(", ")}. Taken back to Terms so you can `
          + `fill it in.`
        : m);
      setErrors(fe);
      setTouched(true);
      setStep(0);
    } finally {
      setBusy("");
    }
  }

  // Which limits to show up front. Seventeen empty rows is a wall — most
  // contracts set five or six of them, and the ones that matter get lost among
  // the ones that do not. So the common ones are here and the rest are one
  // click away, already filled ones always shown.
  const COMMON = [
    // Cover
    "coverage", "territory", "excluded_risks",
    // Authority
    "max_sum_insured", "premium_cap_total", "underwriting_authority",
    // Financial
    "commission_pct", "brokerage_pct", "currency",
  ];
  const [showAllLimits, setShowAllLimits] = useState(false);
  const [showMoreBasics, setShowMoreBasics] = useState(false);
  const [openBecomes, setOpenBecomes] = useState<string | null>(null);

  const visibleLimits = limitSpec.filter(
    l => showAllLimits || COMMON.includes(l.name) || limits[l.name] !== undefined);
  const hiddenCount = limitSpec.length - visibleLimits.length;

  /** The check a single limit currently produces — so the row can show what it
   *  becomes at the moment the number is typed, rather than three steps later. */
  function checkFor(name: string) {
    return preview?.checks.find(c => c.from === name) ?? null;
  }

  function limitRow(l: AgreedLimitSpec) {
    const entry = limits[l.name];
    const sev = entry?.severity ?? l.default_severity;
    const has = entry !== undefined;
    const becomes = openBecomes === l.name ? checkFor(l.name) : null;
    return (
      <div className="lim" key={l.name}>
        <div className="lq">
          <b>{l.question}</b>
          <span>{l.sub}</span>
          {has && l.checkable && (
            <span
              className="linkish" role="button" style={{ fontSize: 11.5 }}
              onClick={() => setOpenBecomes(o => o === l.name ? null : l.name)}
            >
              {openBecomes === l.name ? "Hide" : "What this becomes"}
            </span>
          )}
        </div>
        <div className={l.kind === "choice" ? "" : "unit"}>
          {l.choices ? (
            <select
              value={String(entry?.value ?? "")}
              onChange={e => setLimit(l.name, { value: e.target.value })}
            >
              <option value="">Not agreed</option>
              {l.choices.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          ) : (
            <>
              <input
                value={String(entry?.value ?? "")}
                onChange={e => setLimit(l.name, { value: e.target.value })}
                placeholder="—"
              />
              {(l.unit || l.kind === "money") && (
                <em>{l.unit
                     || String(limits.currency?.value ?? "").toUpperCase()
                     || "—"}</em>
              )}
            </>
          )}
        </div>
        <div>
          {/* The severity question only exists once there is a limit to break.
              Asking "if a file breaks it" of an empty row is asking about
              nothing. */}
          {!has ? (
            <span className="sub">—</span>
          ) : l.checkable ? (
            <div className="segpick">
              <button
                type="button" className={sev === "critical" ? "on" : ""}
                onClick={() => setLimit(l.name, { severity: "critical" })}
              >
                Stop the row
              </button>
              <button
                type="button" className={sev === "warning" ? "on" : ""}
                onClick={() => setLimit(l.name, { severity: "warning" })}
              >
                Just flag it
              </button>
            </div>
          ) : (
            <span className="sub">
              Goes in the wording. Nothing in a file to check it against.
            </span>
          )}
        </div>
        {becomes && (
          <div className="lim-becomes">
            <b>In the contract:</b> the clause quoting{" "}
            <span className="term">{preview?.tokens[l.name]}</span>
            <span className="arrow">→</span>
            <b>On every row:</b>{" "}
            <span className="mono">{becomes.expression}</span>
            <span className="arrow">→</span>
            <span className={`badge ${becomes.severity === "critical" ? "b-crit" : "b-warn"}`}>
              <span className="d" />
              {becomes.severity === "critical" ? "Stop the row" : "Flag it"}
            </span>
          </div>
        )}
      </div>
    );
  }


  const active = sections?.[activeSection];

  return (
    <div className="proto">
      <div className="view full">
        {kind !== "endorse" && (
        <div className="trail">
          {STEPS.map((s, i) => (
            <span key={s.key} style={{ display: "contents" }}>
              {i > 0 && <span className="sep">›</span>}
              <span
                className={`step-c ${i === step ? "on" : i < furthest || i < step ? "done" : "off"}`}
                onClick={() => i <= furthest && go(i)}
              >
                <span className="n">{i + 1}</span>
                <span className="lvbox">
                  <span className="lv">Step {i + 1}</span>
                  <span className="nm">{s.label}</span>
                </span>
              </span>
            </span>
          ))}
        </div>
        )}

        {/* ══ STEP 1 · TERMS ══ */}
        {step === 0 && (
          <div>
            <div className="page-head">
              <div className="t">
                <h2>{kind === "endorse" ? "Endorse a Contract" : "Create a Contract"}</h2>
                <p>
                  {kind === "endorse"
                    ? "Change some terms of a contract that is already running. "
                      + "Kavachio writes the endorsement, attaches it beside the "
                      + "wording, and moves the checks — nothing is attached "
                      + "until you press the button."
                    : "Answer these questions once. Kavachio writes the "
                      + "document, you read it through, then it goes out for "
                      + "signature — and nothing is sent to anybody until that "
                      + "last step."}
                </p>
              </div>
              <div className="actions">
                <Link to="/contracts" className="btn">
                  <ArrowLeft size={14} /> Contracts
                </Link>
                {kind !== "endorse" && (
                  <button className="btn pri" type="button" onClick={() => go(1)}>
                    Next: the wording →
                  </button>
                )}
              </div>
            </div>

            {message && (
              <div className="note warn" style={{ marginBottom: 16 }}>{message}</div>
            )}

            <div className="card pad">
              <div className="fh">
                What are you making?
                <em>they end in different places, so this is the first question</em>
              </div>
              <div className="startpick">
                {KINDS.map(k => (
                  <div
                    key={k.key}
                    className={`sp ${kind === k.key ? "on" : ""}`}
                    onClick={() => setKind(k.key)}
                  >
                    <b>{k.title}</b><span>{k.sub}</span>
                  </div>
                ))}
              </div>
              {kind === "renew" && (
                <div className="note" style={{ marginTop: 12 }}>
                  <b>Which contract is this the renewal of?</b>
                  <p style={{ margin: "4px 0 10px" }}>
                    Its terms, limits and wording come across so the broker only
                    reads the numbers that moved. Last year's contract is not
                    touched — everything already checked against it keeps its
                    meaning.
                  </p>
                  <div className="field" style={{ marginBottom: 0, maxWidth: 460 }}>
                    <select
                      value={renewsId}
                      onChange={e => loadRenewalSource(e.target.value)}
                    >
                      <option value="">Select the contract being renewed…</option>
                      {renewable.map(c => (
                        <option key={c.id} value={String(c.id)}>
                          {c.name}
                          {c.counterparty ? ` — ${c.counterparty.name}` : ""}
                          {c.expiry_dt ? ` (ends ${c.expiry_dt})` : ""}
                        </option>
                      ))}
                    </select>
                    {busy === "renewal" && (
                      <div className="hint">Bringing its terms across…</div>
                    )}
                  </div>
                </div>
              )}
              {kind === "endorse" && (
                <div className="note" style={{ marginTop: 12 }}>
                  <b>Which contract are you changing?</b>
                  <p style={{ margin: "4px 0 10px" }}>
                    An endorsement is not a new contract. This one keeps
                    running, keeps its id and keeps every bordereau already
                    checked against it — you change some of its terms from a
                    date, and both documents stay in force together.
                  </p>
                  <div className="grid g-3">
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Contract to endorse</label>
                      <select value={endorseId}
                              onChange={e => loadEndorseSource(e.target.value)}>
                        <option value="">Select a live contract…</option>
                        {endorsable.map(c => (
                          <option key={c.id} value={String(c.id)}>
                            {c.name}
                            {c.counterparty ? ` — ${c.counterparty.name}` : ""}
                          </option>
                        ))}
                      </select>
                      {endorsable.length === 0 && (
                        <div className="hint">
                          Nothing is running yet. Only a contract in force can
                          be endorsed.
                        </div>
                      )}
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Takes effect from</label>
                      <input type="date" value={endorseFrom}
                             onChange={e => setEndorseFrom(e.target.value)} />
                      <div className="hint">
                        Business before this date is judged on the old terms.
                      </div>
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>
                        Reason{" "}
                        <span className="muted" style={{ fontWeight: 500 }}>
                          — optional
                        </span>
                      </label>
                      <input value={endorseNote}
                             placeholder="e.g. Agreed at the mid-year review"
                             onChange={e => setEndorseNote(e.target.value)} />
                    </div>
                  </div>
                  {busy === "endorse-load" && (
                    <div className="hint">Loading its current terms…</div>
                  )}
                </div>
              )}

              <div className="divider" />

              {kind === "endorse" ? (
                endorseId && endorsePv && (
                  <>
                    <div className="fh">
                      The contract you are changing
                      <em>none of this moves — an endorsement changes terms, not parties</em>
                    </div>
                    <div className="grid g-3" style={{ marginBottom: 18 }}>
                      <div className="field" style={{ marginBottom: 0 }}>
                        <label>Contract</label>
                        <input className="ro" readOnly
                               value={endorsePv.contract.name} />
                      </div>
                      <div className="field" style={{ marginBottom: 0 }}>
                        <label>Broker</label>
                        <input className="ro" readOnly
                               value={counterparty?.name ?? "—"} />
                      </div>
                      <div className="field" style={{ marginBottom: 0 }}>
                        <label>Its term</label>
                        <input className="ro" readOnly
                               value={endorsePv.contract.inception_dt
                                 ? `${endorsePv.contract.inception_dt} → ${endorsePv.contract.expiry_dt}`
                                 : "—"} />
                      </div>
                    </div>
                  </>
                )
              ) : (
              <>
              <div className="fh">
                The basics <em>who the contract is with, and how long it runs</em>
              </div>
              <div className="grid g-3">
                <div className="field">
                  <label>Carrier</label>
                  <input className="ro" value={carrierName} readOnly />
                </div>
                <div className="field">
                  <label>Programme</label>
                  <select
                    value={programId}
                    onChange={e => { setProgramId(e.target.value); setBrokerId(""); }}
                    style={touched && !programId
                      ? { borderColor: "var(--p-crit)" } : undefined}
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
                    style={touched && !brokerId
                      ? { borderColor: "var(--p-crit)" } : undefined}
                  >
                    <option value="">
                      {programId ? "Select a broker…" : "Choose a programme first"}
                    </option>
                    {(counterparties ?? []).map(c => (
                      <option key={c.id} value={String(c.id)}>{c.name}</option>
                    ))}
                  </select>

                </div>
              </div>



              {/* Driven by the SERVER's field spec, not a hand-written list.
                  Hardcoding these is what let the form call class of business
                  optional while the server required it — the exact drift the
                  spec is served to prevent.

                  Required inline; the rest folded away. Nine optional inputs
                  shown flat made the required ones hard to find, and a contract
                  that needs none of them should not have to scroll past them. */}
              <div className="grid g-3">
                {basicFields.filter(f => f.required).map(input)}
              </div>

              {optionalBasics.length > 0 && (
                <div style={{ marginTop: 4 }}>
                  <button
                    className="btn sm" type="button"
                    onClick={() => setShowMoreBasics(v => !v)}
                  >
                    {showMoreBasics
                      ? "Hide the rest"
                      : `＋ More about this contract (${optionalBasics.length})`}
                  </button>
                  <span className="sub" style={{ marginLeft: 10 }}>
                    Reference, risk code, year of account and the like — leave
                    them out and nothing asks again.
                    {filledOptional > 0 && ` ${filledOptional} set.`}
                  </span>
                  {showMoreBasics && (
                    <div className="grid g-3" style={{ marginTop: 14 }}>
                      {optionalBasics.map(input)}
                    </div>
                  )}
                </div>
              )}
              </>
              )}

              {(kind !== "endorse" || endorseId) && <div className="divider" />}

              <div className="fh">
                {kind === "endorse" ? "The terms — change what moved" : "The limits you agreed"}
                <em>
                  {kind === "endorse"
                    ? "these are its current terms — edit the ones the "
                      + "endorsement changes and leave the rest alone"
                    : "each line becomes a clause in the contract and a check "
                      + "that runs on every file the broker sends"}
                </em>
              </div>
              {limitGroups.map(g => {
                const rows = visibleLimits.filter(l => l.group === g.key);
                if (!rows.length) return null;
                return (
                  <div key={g.key} style={{ marginBottom: 8 }}>
                    <div className="sub-h" style={{ marginTop: 14 }}>
                      {g.label}
                    </div>
                    <div className="hint" style={{ marginTop: 0, marginBottom: 6 }}>
                      {g.sub}
                    </div>
                    <div className="lim lim-h">
                      <div className="lq"><b>What you agreed</b></div>
                      <div className="sub">The limit</div>
                      <div className="sub">If a file breaks it</div>
                    </div>
                    {rows.map(limitRow)}
                  </div>
                );
              })}

              {hiddenCount > 0 && (
                <div style={{ paddingTop: 12 }}>
                  <button
                    className="btn sm" type="button"
                    onClick={() => setShowAllLimits(v => !v)}
                  >
                    {showAllLimits
                      ? "Show fewer"
                      : `＋ ${hiddenCount} more you can set`}
                  </button>
                  <span className="sub" style={{ marginLeft: 10 }}>
                    Brokerage, profit commission, settlement, tax — leave them
                    out and the contract simply does not mention them.
                  </span>
                </div>
              )}

              {kind !== "endorse" && (
              <>
              <div className="divider" />
              <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                <span className="badge b-ok" style={{ flex: "0 0 auto", marginTop: 1 }}>
                  <span className="d" />
                  {preview?.checks.length
                    ?? (Object.entries(limits).filter(([k]) =>
                          limitSpec.find(l => l.name === k)?.checkable).length
                        + (values.inception_dt && values.expiry_dt ? 1 : 0))}{" "}
                  checks
                </span>
                <span className="muted" style={{ fontSize: 12.5, lineHeight: 1.6 }}>
                  One for each limit above that a spreadsheet can be measured
                  against, plus one from the dates — a risk that starts outside
                  the contract term is not covered by it. Because you typed these
                  numbers rather than us reading them out of somebody else's PDF,
                  every one of them can be checked automatically.
                </span>
              </div>
              </>
              )}
            </div>

            {/* An endorsement finishes here — there is no wording to write
                from scratch and no signature setup to do, only the change and
                the document that records it. */}
            {kind === "endorse" && endorseId && (
              <div className="card" style={{ marginTop: 18 }}>
                <div className="card-h">
                  <h3>
                    What this endorsement says
                    {endorsePv && <> · Endorsement {endorsePv.number}</>}
                  </h3>
                  <span className="sub">
                    nothing is attached until you press the button
                  </span>
                </div>
                {!endorsePv || endorsePv.changes.length === 0 ? (
                  <div className="empty">
                    Change a term above and it will appear here.
                  </div>
                ) : (
                  <>
                    <div className="tbl-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Term</th><th>Was</th><th>Becomes</th>
                            <th>The check it runs</th>
                          </tr>
                        </thead>
                        <tbody>
                          {endorsePv.changes.map(ch => (
                            <tr key={ch.key}>
                              <td>
                                <b>{ch.question}</b>
                                <div className="sub">{ch.kind}</div>
                              </td>
                              <td className="mono">{ch.from || "—"}</td>
                              <td className="mono">{ch.to || "—"}</td>
                              <td>
                                {ch.check_after ? (
                                  <>
                                    <span className="mono">{ch.check_after}</span>
                                    {ch.check_before
                                     && ch.check_before !== ch.check_after && (
                                      <div className="sub mono">
                                        was {ch.check_before}
                                      </div>
                                    )}
                                  </>
                                ) : (
                                  <span className="sub">
                                    Recorded in the wording — nothing to check
                                  </span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div style={{ padding: "16px 20px" }}>
                      {endorsePv.sections.map(sec => (
                        <div key={sec.key} style={{ marginBottom: 12 }}>
                          <b style={{ fontSize: 13 }}>{sec.title}</b>
                          <div className="muted" style={{ fontSize: 13,
                               lineHeight: 1.7, whiteSpace: "pre-line",
                               marginTop: 4 }}>
                            {sec.body}
                          </div>
                        </div>
                      ))}
                      <div className="note">
                        <b>Both documents stay in force.</b> The endorsement is
                        attached beside the wording, not instead of it — so the
                        contract still says what it always said, and this says
                        what changed. Its rules are rebuilt from the pair when
                        you re-read the contract.
                      </div>
                      <div className="rowacts">
                        <button
                          className="btn pri" type="button"
                          disabled={!!busy || !endorsePv.changes.length}
                          onClick={endorse}
                        >
                          {busy === "endorse"
                            ? "Endorsing…"
                            : `Endorse ${endorsePv.contract.name}`}
                        </button>
                        <Link to={`/contracts/${endorseId}`} className="linkish">
                          Open the contract instead →
                        </Link>
                      </div>
                    </div>
                  </>
                )}
              </div>
            )}

            {kind !== "endorse" && (
              <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.6,
                   textAlign: "center", margin: "18px auto 0", maxWidth: 640 }}>
                Already have a contract somebody else drafted?{" "}
                <Link to="/contracts/upload" className="linkish">
                  Upload the signed PDF instead →
                </Link>{" "}
                Kavachio reads the terms out of it and builds what checks it can
                — but a contract written from its own terms never has a clause
                the checks cannot read.
              </p>
            )}
          </div>
        )}

        {/* ══ STEP 2 · WORDING ══ */}
        {step === 1 && (
          <>
            <div className="page-head">
              <div className="t">
                <h2>Write the Wording</h2>
                <p>
                  {values.name || "This contract"}
                  {counterparty && <>, for {counterparty.name}</>}
                  {programme && <> on {programme.name}</>}.{" "}
                  {sections?.length ?? 0} sections, written for you already.
                </p>
              </div>
              <div className="actions">
                <button className="btn" type="button" onClick={() => go(0)}>
                  ← Terms
                </button>
                <button
                  className="btn" type="button"
                  onClick={() => setSections(s => {
                    const next = [...(s ?? []), {
                      key: `custom_${Date.now()}`, title: "New section",
                      body: "Write this section in your own words.",
                      origin: "your own words" }];
                    setActiveSection(next.length - 1);
                    return next;
                  })}
                >
                  <Plus size={14} /> Add a section
                </button>
                <button className="btn pri" type="button" onClick={() => go(2)}>
                  Read it through →
                </button>
              </div>
            </div>

            <div className="note" style={{ marginBottom: 18 }}>
              <b>Kavachio has written the wording from your terms.</b> The shaded
              bits are <b>live values</b>, not typed text — go back and change the
              commission cap and every one of them updates, and so does the check
              behind it. Anything you type yourself stays exactly as you typed it.
            </div>

            <div className="doc" style={{ marginBottom: 18 }}>
              <div className="doc-rail">
                {(sections ?? []).map((s, i) => (
                  <div
                    key={s.key}
                    className={`doc-sec ${i === activeSection ? "on" : ""}`}
                    onClick={() => setActiveSection(i)}
                  >
                    <span className="no">§{i + 1}</span>
                    <span className="txt">
                      <span className="nm">{s.title}</span>
                      <span className="st">{s.origin}</span>
                    </span>
                    {s.locked ? (
                      <span className="keep" title="Every contract has to say who the parties are — this one cannot be removed">🔒</span>
                    ) : (
                      <span
                        className="rm" title="Delete this section"
                        onClick={e => {
                          e.stopPropagation();
                          setSections(list => (list ?? []).filter((_, j) => j !== i));
                          setActiveSection(a => Math.max(0, a - (i <= a ? 1 : 0)));
                        }}
                      >
                        ×
                      </span>
                    )}
                  </div>
                ))}
              </div>

              <div className="doc-body">
                {active ? (
                  <>
                    <h4>§{activeSection + 1} &nbsp; {active.title}</h4>
                    <p className="muted" style={{ fontSize: 12.5, margin: "0 0 14px" }}>
                      Click into the text and type. This is the wording that will
                      appear in the signed contract. To remove a whole section,
                      hover it in the list on the left and click the <b>×</b>.
                    </p>
                    <div className="field" style={{ marginBottom: 12 }}>
                      <label>Section title</label>
                      <input
                        value={active.title}
                        onChange={e => setSections(list => (list ?? []).map(
                          (x, j) => j === activeSection
                            ? { ...x, title: e.target.value } : x))}
                      />
                    </div>
                    <WordingEditor
                      sectionKey={`${active.key}:${wordingVersion}`}
                      body={active.body}
                      tokens={preview?.tokens ?? {}}
                      labels={tokenLabels}
                      onInsertRequest={fn => { insertChip.current = fn; }}
                      onChange={body => setSections(list => (list ?? []).map(
                        (x, j) => j === activeSection
                          ? { ...x, body,
                              origin: x.origin === "from your terms"
                                ? "from your terms · edited" : x.origin }
                          : x))}
                    />

                    <div className="termbar">
                      <span className="muted" style={{ fontSize: 11.5,
                            alignSelf: "center", marginRight: 2 }}>
                        Drop in a live value:
                      </span>
                      {Object.keys(preview?.tokens ?? {}).map(t => (
                        <button
                          key={t} type="button"
                          onClick={() => insertChip.current?.(t)}
                        >
                          {tokenLabels[t] ?? t}
                        </button>
                      ))}
                    </div>

                    <div className="note ok" style={{ marginTop: 16 }}>
                      <b>Why the shaded values matter.</b> A chip is tied to the
                      term you set in step 1. If the cap moves to 12.5%, this
                      sentence changes with it and so does the check behind it.
                      Type “15%” by hand instead and the two quietly drift
                      apart — which is how a contract ends up saying one thing
                      while the system checks another.
                    </div>
                  </>
                ) : (
                  <div className="empty">
                    No sections yet. Add one, or go back and set some terms.
                  </div>
                )}
              </div>
            </div>

            <div className="rowacts">
              <button className="btn" type="button" onClick={() => refresh(sections)}>
                Refresh from my terms
              </button>
              <button className="btn" type="button"
                      onClick={() => refresh(null)}>
                Start the wording again
              </button>
              <span className="sub">
                “Start again” throws away your edits and rewrites every section
                from the terms.
              </span>
            </div>
          </>
        )}

        {/* ══ STEP 3 · READ IT THROUGH ══ */}
        {step === 2 && preview && (
          <>
            <div className="page-head">
              <div className="t">
                <h2>Read It Through</h2>
                <p>
                  {values.name} · {preview.pages} pages · {preview.sections.length}{" "}
                  sections · {preview.checks.length} automatic checks.{" "}
                  <b>Nothing has been sent yet.</b>
                </p>
              </div>
              <div className="actions">
                <button className="btn" type="button" onClick={() => go(1)}>
                  ← Back to the wording
                </button>
                <button
                  className="btn" type="button" disabled={!!busy}
                  onClick={async () => {
                    setBusy("draft");
                    try { await downloadDraft(wordingInput(sections)); }
                    catch (e) { setMessage(fieldErrors(e).message); }
                    finally { setBusy(""); }
                  }}
                >
                  <Download size={14} />
                  {busy === "draft" ? "Composing…" : "Download the draft"}
                </button>
                <button className="btn pri" type="button" onClick={() => go(3)}>
                  Set up signatures →
                </button>
              </div>
            </div>

            <div className="tiles" style={{ marginBottom: 18 }}>
              <div className="tile">
                <div className="k">Pages</div>
                <div className="v">{preview.pages}</div>
                <div className="foot">including the signature page</div>
              </div>
              <div className="tile">
                <div className="k">Sections</div>
                <div className="v">{preview.sections.length}</div>
                <div className="foot">
                  {preview.sections.filter(s => s.origin === "your own words").length}{" "}
                  you wrote yourself
                </div>
              </div>
              <div className="tile">
                <div className="k">Checks it will run</div>
                <div className="v">{preview.checks.length}</div>
                <div className="foot">on every row of every file</div>
              </div>
              <div className={`tile ${preview.warnings.length ? "warnl" : ""}`}>
                <div className="k">Worth a look first</div>
                <div className="v" style={preview.warnings.length
                  ? { color: "var(--p-warn)" } : undefined}>
                  {preview.warnings.length + preview.uncheckable.length}
                </div>
                <div className="foot">neither one blocks you</div>
              </div>
            </div>

            {(preview.warnings.length > 0 || preview.uncheckable.length > 0) && (
              <div className="card" style={{ marginBottom: 18 }}>
                <div className="card-h">
                  <h3>Two things worth knowing</h3>
                  <span className="sub">neither stops you sending it</span>
                </div>
                <div style={{ padding: "16px 20px" }}>
                  {preview.uncheckable.map((u, i) => (
                    <div className="kv" key={`u${i}`} style={{ alignItems: "flex-start" }}>
                      <span className="k">
                        <b style={{ color: "var(--p-ink)" }}>
                          {u.title} will not be checked
                        </b>
                        <div className="sub">
                          It is a real term and it stays in the contract, but it
                          quotes nothing a spreadsheet can be compared against,
                          so no check comes out of it.
                        </div>
                      </span>
                    </div>
                  ))}
                  {preview.warnings.map((w, i) => (
                    <div className="kv" key={`w${i}`} style={{ alignItems: "flex-start" }}>
                      <span className="k">
                        <b style={{ color: "var(--p-ink)" }}>{w.title}</b>
                        <div className="sub">{w.detail}</div>
                      </span>
                    </div>
                  ))}
                  <div className="note" style={{ marginTop: 12 }}>
                    Nothing here is an error. Both are the kind of thing somebody
                    notices three months later and asks about, so it is cheaper
                    to see them now.
                  </div>
                </div>
              </div>
            )}

            <div className="card" style={{ marginBottom: 18 }}>
              <div className="card-h">
                <h3>The document</h3>
                <span className="sub">exactly what the broker will open</span>
              </div>
              <div style={{ padding: "16px 20px" }}>
                {preview.sections.map((s, i) => (
                  <div key={s.key} style={{ marginBottom: 14 }}>
                    <b style={{ fontSize: 13 }}>§{i + 1} &nbsp; {s.title}</b>
                    <div className="muted" style={{ fontSize: 13, lineHeight: 1.7,
                         whiteSpace: "pre-line", marginTop: 4 }}>
                      {s.rendered}
                    </div>
                  </div>
                ))}
                <div className="note">
                  <b>Kavachio adds the signature page.</b> Both signature blocks
                  are placed on it for you. You can move them in the next step if
                  your broker's lawyers want them somewhere else.
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-h">
                <h3>The checks it will run</h3>
                <span className="sub">
                  from the moment both parties sign
                </span>
              </div>
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr><th>From</th><th>What gets checked</th><th>How serious</th></tr>
                  </thead>
                  <tbody>
                    {preview.checks.map((c, i) => (
                      <tr key={i}>
                        <td>{c.title}</td>
                        <td className="mono">{c.expression}</td>
                        <td>
                          <span className={`badge ${c.severity === "critical" ? "b-crit" : "b-warn"}`}>
                            <span className="d" />
                            {c.severity === "critical" ? "Stop the row" : "Flag it"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="note" style={{ margin: 0, borderRadius: 0,
                   borderLeft: 0, borderRight: 0, borderBottom: 0 }}>
                These do nothing until the contract is created and in force. A
                draft never checks anything.
              </div>
            </div>
          </>
        )}

        {/* ══ STEP 4 · SIGNATURES ══
            The design's step 4 sends the contract out for signature. Kavachio
            cannot send anything yet, so this step does the part that IS real:
            it names who signs and in what order, and stores that with the
            contract so its signature screen opens with the list already in it.
            Then it creates the contract. Saying that plainly is better than a
            "Send for signature" button that quietly does nothing. */}
        {step === 3 && (
          <>
            <div className="page-head">
              <div className="t">
                <h2>Signatures</h2>
                <p>
                  {values.name} · naming who will sign. Nobody has signed yet —
                  that happens on the contract's signature page, and both sides
                  signing is what puts it in force.
                </p>
              </div>
              <div className="actions">
                <button className="btn" type="button" onClick={() => go(2)}>
                  ← Read it through
                </button>
                {/* Two ways to finish, and neither puts the contract in
                    force. A contract goes live from its own page, once it has
                    a wording somebody has read and — where there is a broker —
                    terms they agreed to, and — always — signatures from both
                    sides. Offering "make it live" here made the last click of
                    an authoring flow the moment checks start running on real
                    bordereaux, on a contract nobody had signed. */}
                <button className="btn" type="button" disabled={!!busy}
                        onClick={() => create("draft")}>
                  {busy === "create" ? "Saving…" : "Save as a draft"}
                </button>
                <button className="btn pri" type="button" disabled={!!busy}
                        onClick={() => create("review")}>
                  <FileText size={14} />
                  {busy === "create"
                    ? "Creating…" : "Create and send it to the broker"}
                </button>
              </div>
            </div>

            {message && (
              <div className="note warn" style={{ marginBottom: 16 }}>{message}</div>
            )}

            <div className="grid g-12">
              <div className="card">
                <div className="card-h">
                  <h3>Who signs it</h3>
                  <span className="sub">they sign in the order shown</span>
                </div>
                <div style={{ padding: "16px 20px" }}>
                  {signers.length === 0 ? (
                    <div className="empty" style={{ padding: "18px 10px" }}>
                      Nobody named yet. You can add them later on the contract's
                      own signature screen.
                    </div>
                  ) : signers.map((sg, i) => (
                    <div className="kv" key={i}>
                      <span className="k">
                        <span className="badge b-mut" style={{ marginRight: 8 }}>
                          <span className="d" />{i + 1}
                        </span>
                        <b style={{ color: "var(--p-ink)" }}>{sg.name}</b>
                        <div className="sub">
                          {sg.side === "carrier"
                            ? carrierName
                            : counterparty?.name ?? "the broker"}
                          {sg.role ? ` · ${sg.role}` : ""} · {sg.email}
                        </div>
                      </span>
                      <span
                        className="linkish" role="button"
                        onClick={() => setSigners(l => l.filter((_, j) => j !== i))}
                      >
                        Remove
                      </span>
                    </div>
                  ))}

                  <div className="divider" />
                  <div className="grid g-3">
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Signs for</label>
                      <select value={sgSide}
                              onChange={e => setSgSide(e.target.value as "carrier" | "counterparty")}>
                        <option value="carrier">{carrierName} (you)</option>
                        <option value="counterparty">
                          {counterparty?.name ?? "The broker"}
                        </option>
                      </select>
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Name</label>
                      <input value={sgName}
                             onChange={e => setSgName(e.target.value)} />
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Email</label>
                      <input type="email" value={sgEmail}
                             onChange={e => setSgEmail(e.target.value)} />
                    </div>
                  </div>
                  <div className="rowacts">
                    <button
                      className="btn sm" type="button"
                      disabled={!sgName.trim() || !sgEmail.trim()}
                      onClick={() => {
                        setSigners(l => [...l, {
                          name: sgName.trim(), email: sgEmail.trim(),
                          role: sgRole.trim() || undefined, side: sgSide }]);
                        setSgName(""); setSgEmail(""); setSgRole("");
                      }}
                    >
                      <Plus size={12} /> Add signer
                    </button>
                    <span className="sub">
                      Optional. Naming them now just saves doing it later.
                    </span>
                  </div>
                </div>
              </div>

              <div className="card pad">
                <h3 style={{ margin: "0 0 12px", fontSize: 14 }}>
                  What happens when you press the button
                </h3>
                <div className="kv">
                  <span className="k">
                    <b style={{ color: "var(--p-ink)" }}>Save as a draft</b>
                    <div className="sub">
                      Written down and nothing more. Not live, nothing checked,
                      and every term still editable — so you can come back to it,
                      or hand it to a colleague to finish. Nobody else sees it.
                    </div>
                  </span>
                </div>
                <div className="kv">
                  <span className="k">
                    <b style={{ color: "var(--p-ink)" }}>Create and send it to the broker</b>
                    <div className="sub">
                      They read the terms and either agree them or ask for
                      changes. The contract exists either way, but it is not in
                      force and no check runs until they have agreed.
                    </div>
                  </span>
                </div>
                <div className="kv">
                  <span className="k">
                    <b style={{ color: "var(--p-ink)" }}>Neither one makes it live</b>
                    <div className="sub">
                      A contract goes in force when <b>both sides have signed
                      it</b>, and nothing else does that. You sign on the
                      contract's signature page, the other side signs theirs,
                      and the second signature puts it in force — at which point
                      its checks start running on real bordereaux.
                    </div>
                  </span>
                </div>

                {/* <div className="divider" /> */}
                {/* <div className="note warn">
                  <b>Kavachio does not email anyone to sign.</b> The signature
                  round is not wired to a provider yet, so nothing goes out
                  today. What this step does is record who signs — the
                  contract's own Signature screen opens with that list, and the
                  broker returns the executed copy from there.
                </div> */}

                {/* <div className="divider" /> */}
                <div className="kv">
                  <span className="k">Contract</span>
                  <span className="v">{values.name}</span>
                </div>
                <div className="kv">
                  <span className="k">With</span>
                  <span className="v">{counterparty?.name ?? "—"}</span>
                </div>
                <div className="kv">
                  <span className="k">Sections</span>
                  <span className="v">{sections?.length ?? 0}</span>
                </div>
                <div className="kv">
                  <span className="k">Checks</span>
                  <span className="v">{preview?.checks.length ?? 0}</span>
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
