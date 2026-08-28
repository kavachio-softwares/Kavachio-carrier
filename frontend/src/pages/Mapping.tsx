import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { currentMga } from "../auth";

// Strip optional role suffix so spec entries with the base form still match
// against candidates that include the role-prefixed variant (and vice versa).
function canonicalBase(k: string): string {
  return k.replace(/_(insured|agency|carrier)$/, "");
}
function confidenceFor(current: string | undefined, candidates: Candidate[]): number {
  if (!current || candidates.length === 0) return candidates[0]?.confidence ?? 0;
  const exact = candidates.find(c => c.canonical === current);
  if (exact) return exact.confidence;
  const cb = canonicalBase(current);
  const loose = candidates.find(c => canonicalBase(c.canonical) === cb);
  if (loose) return loose.confidence;
  return candidates[0]?.confidence ?? 0;
}

type Candidate = { canonical: string; confidence: number; reason: string };
type Mapper = {
  id: number; mga: string; carrier?: string; approved: boolean;
  signature: string[];
  spec_by_sheet: Record<string, Record<string, string | string[]>>;
  candidates: Record<string, Candidate[]>;
  samples?: Record<string, string[]>;
  output_by_source?: Record<string, string>;
};

const SHEET_SEP = " :: ";

export default function Mapping() {
  const { mapperId } = useParams();
  const nav = useNavigate();
  // When opened from the admin Data Mapping Queue the caller passes ?return=…
  // so ← Queue / post-approve land back where they came from.
  const [params] = useSearchParams();
  const returnTo = params.get("return");
  // When opened from a mapping task, ?task=… lets Approve & save close the task
  // and kick off the background backfill of its pending files.
  const taskId = params.get("task");
  const mga = currentMga();
  const [m, setM] = useState<Mapper | null>(null);
  const [model, setModel] = useState<Record<string, any>>({});
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // Snapshot of the last-loaded/last-saved mapping — compared against the live
  // edits to disable Save Draft / Approve & Save until something actually changed.
  const [savedSpec, setSavedSpec] = useState<Mapper["spec_by_sheet"] | null>(null);

  useEffect(() => {
    api.get<Mapper>(`/mapper/${mapperId}`).then(r => { setM(r.data); setSavedSpec(r.data.spec_by_sheet ?? {}); });
    api.get(`/data-model`).then(r => setModel(r.data));
  }, [mapperId]);

  // Source → canonical (from spec). Walks spec_by_sheet (source can be a string
  // or a list of strings) and inverts to source-keyed. Keep hooks above returns.
  const currentBySource = useMemo(() => {
    const out: Record<string, string> = {};
    if (!m) return out;
    for (const sheet of Object.keys(m.spec_by_sheet ?? {})) {
      for (const [canon, src] of Object.entries(m.spec_by_sheet[sheet] ?? {})) {
        const list = Array.isArray(src) ? src : [src];
        for (const s of list) out[s] = canon;
      }
    }
    return out;
  }, [m]);

  // Flat, ordered list of source columns (preserving each sheet's grouping).
  const sources = useMemo(() => {
    const out: { src: string; sheet: string; col: string }[] = [];
    if (!m) return out;
    const seen = new Set<string>();
    const add = (src: string) => {
      if (!src || seen.has(src)) return;
      seen.add(src);
      const [sheet, col] = src.split(SHEET_SEP);
      out.push({ src, sheet, col: col ?? sheet });
    };
    for (const src of Object.keys(m.candidates ?? {})) add(src);
    for (const sheet of Object.keys(m.spec_by_sheet ?? {})) {
      for (const v of Object.values(m.spec_by_sheet[sheet] ?? {})) {
        const list = Array.isArray(v) ? v : [v];
        for (const s of list) add(s as string);
      }
    }
    if (seen.size === 0) for (const tok of m.signature ?? []) add(tok);
    return out.sort((a, b) =>
      a.sheet === b.sheet ? a.col.localeCompare(b.col) : a.sheet.localeCompare(b.sheet));
  }, [m]);

  // Every canonical data-model field the mapper can target (bdx + fk_resolve),
  // offered under an "All fields" group so any column can reach any field.
  const allFields = useMemo(
    () => Object.entries(model)
      .filter(([, v]: any) => v.source === "bdx" || v.source === "fk_resolve")
      .map(([k]) => k)
      .sort(),
    [model]);

  const multiSheet = useMemo(
    () => new Set(sources.map(s => s.sheet)).size > 1, [sources]);

  const dirty = useMemo(
    () => JSON.stringify(m?.spec_by_sheet ?? {}) !== JSON.stringify(savedSpec ?? {}),
    [m, savedSpec]);
  // Approving for the first time (a freshly-proposed "Mapping Drafted" mapper,
  // approved===false) is itself a real change — flips approved false→true —
  // so it shouldn't require an edit too. Re-approving an already-approved
  // mapper with nothing changed is normally a true no-op — EXCEPT when opened
  // from a task (taskId set): the mapper can already be approved (saved fine)
  // while the task itself failed to close (see the catch below), so the button
  // must stay usable to retry that close — there's no dirty/approved signal
  // for "task still open" to check instead.
  const nothingToApprove = !taskId && !dirty && !!m?.approved;

  if (!m) return null;

  function applyChoice(srcCol: string, canonical: string | null) {
    const [sheet] = srcCol.split(SHEET_SEP);
    setM(prev => {
      if (!prev) return prev;
      const sbs = { ...(prev.spec_by_sheet ?? {}) };
      // Remove any existing canonical → srcCol link across ALL sheet buckets —
      // an existing mapping can live under a different bucket than srcCol's own
      // sheet prefix; if we only cleared one bucket the stale link could survive
      // and win in currentBySource, so changing a set field wouldn't "stick".
      for (const sh of Object.keys(sbs)) {
        const map = { ...(sbs[sh] ?? {}) };
        let changed = false;
        for (const [c, s] of Object.entries(map)) {
          const list = Array.isArray(s) ? s : [s];
          if (list.includes(srcCol)) {
            const filtered = list.filter(x => x !== srcCol);
            if (filtered.length === 0) delete map[c];
            else map[c] = filtered.length === 1 ? filtered[0] : filtered;
            changed = true;
          }
        }
        if (changed) sbs[sh] = map;
      }
      // Add the new mapping under the source's own input-sheet bucket.
      if (canonical) {
        const map = { ...(sbs[sheet] ?? {}) };
        map[canonical] = srcCol;
        sbs[sheet] = map;
      }
      return { ...prev, spec_by_sheet: sbs };
    });
  }

  async function save(approve: boolean) {
    if (!m) return;
    setBusy(true); setMsg(null);
    try {
      const { data } = await api.put(`/mapper/${m.id}`, {
        spec_by_sheet: m.spec_by_sheet, approved: approve || m.approved,
      });
      setM(prev => prev ? { ...prev, ...data, candidates: prev.candidates } : data);
      setSavedSpec(data.spec_by_sheet ?? m.spec_by_sheet);
      if (approve) {
        // Opened from a mapping task → close it and start the backfill. The
        // endpoint returns immediately; the pending files load into the data
        // model in the background server-side. The mapper is already saved, so
        // a resolve failure only means the task wasn't closed (surface it).
        if (taskId) {
          try {
            await api.post(`/admin/mapping-tasks/${taskId}/resolve`, {
              action: "approve", mapper_id: m.id, resolved_by: mga,
            });
            setMsg("Approved & saved. Loading the pending files into the data model in the background…");
          } catch {
            setMsg("Mapper approved & saved, but closing the task failed — retry from the queue.");
          }
        } else {
          setMsg("Approved & saved — taking you to your saved mappers.");
        }
      } else {
        setMsg("Draft saved.");
      }
    } catch (e: any) { setMsg(e?.response?.data?.detail ?? e?.message ?? "Save failed."); }
    finally { setBusy(false); }
  }

  const totalCols = sources.length;
  const mappedCols = sources.filter(s => currentBySource[s.src]).length;
  const toConfirm = totalCols - mappedCols;

  const q = search.trim().toLowerCase();
  const rows = q
    ? sources.filter(s =>
      s.col.toLowerCase().includes(q) ||
      currentBySource[s.src]?.toLowerCase().includes(q) ||
      (m.candidates?.[s.src] ?? []).some(c => c.canonical.toLowerCase().includes(q)))
    : sources;

  const fmtName = m.carrier || (m as any).source_filename?.replace(/\.[^.]+$/, "") || `Mapper #${m.id}`;

  // Label a data-model field with its table, e.g. "policy · policy_number", using
  // a centered middle dot (·) as the separator so it sits between the two, not on
  // the baseline. Shows which table each field belongs to — not just the field.
  const fieldLabel = (key: string): string => {
    const meta = model[key];
    return meta?.table ? `${meta.table} · ${key}` : key;
  };

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              Map Input Format <span className="tag-pill">{fmtName}</span>
            </h2>
            <p>Match each input column to a field in the Kavachio data model, then approve to save and backfill.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav(returnTo || "/admin/mapping-tasks")}>← Queue</button>
            <button className="btn" onClick={() => save(false)} disabled={busy || !dirty}>
              Save Draft
            </button>
            <button className="btn pri" onClick={() => save(true)} disabled={busy || nothingToApprove}>
              Approve & Save
            </button>
          </div>
        </div>

        {msg && <div className="note ok" style={{ marginBottom: 14, maxWidth: 760 }}>{msg}</div>}

        <div className="card">
          <div className="card-h">
            <h3>Column Mapping</h3>
            <span className="sub">
              {m.mga} · {totalCols} column{totalCols === 1 ? "" : "s"}
            </span>
            <div className="right" style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <input
                className="mini-search"
                placeholder="Search Columns…"
                value={search}
                onChange={e => setSearch(e.target.value)} />
              {toConfirm > 0
                ? <span className="badge b-warn"><span className="d" />{toConfirm} To Confirm</span>
                : <span className="badge b-ok"><span className="d" />All Mapped</span>}
            </div>
          </div>

          <div className="mt-banner">
            Each input column is matched to a canonical <b>Kavachio data-model field</b>.
            Review the suggested matches, update any low-confidence rows, then <b>Approve &amp; Save</b> to
            store the mapping and apply it to pending files.
          </div>

          <div className="mthdr">
            <div>Input Column</div>
            <div>Output Column (by Tenant)</div>
            <div>Kavachio Data-Model Field</div>
            <div>Confidence</div>
            <div>Sample Value</div>
          </div>

          {rows.map(({ src, sheet, col }) => {
            const cands = m.candidates?.[src] ?? [];
            const current = currentBySource[src];
            const conf = current ? confidenceFor(current, cands) : (cands[0]?.confidence ?? 0);
            const pct = Math.round(conf * 100);
            const low = !current || conf < 0.75;
            const sample = (m.samples?.[src] ?? []).filter(Boolean);
            return (
              <div className={`mtrow${low ? " lo" : ""}`} key={src}>
                <div className="src">
                  {col}
                  {multiSheet && <div className="sh">{sheet}</div>}
                </div>
                <div className="out">{m.output_by_source?.[src] || "—"}</div>
                <div>
                  <FieldPicker
                    value={current}
                    suggested={cands.map(c => c.canonical)}
                    allFields={allFields}
                    label={fieldLabel}
                    onPick={k => applyChoice(src, k)}
                  />
                </div>
                <div>
                  {cands.length > 0 || current
                    ? <span className={`conf${pct < 75 ? " lo" : ""}`}>{pct}%</span>
                    : <span className="conf lo">n/a</span>}
                </div>
                <div className="smp" title={sample.join(" · ")}>
                  {sample.length ? sample[0] : "—"}
                </div>
              </div>
            );
          })}

          {rows.length === 0 && (
            <div className="empty">
              {q ? "No columns match your search." : "No source columns found for this mapper."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Searchable data-model field picker: a trigger button + a fixed-position
// dropdown with a search box (fixed so it escapes the card's overflow; flips up
// near the bottom). Only renders its options while open, so 700+ rows stay light.
function FieldPicker({ value, suggested, allFields, label, onPick }: {
  value?: string;
  suggested: string[];
  allFields: string[];
  label: (key: string) => string;
  onPick: (key: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const btnRef = useRef<HTMLButtonElement>(null);
  const [box, setBox] = useState<
    { left: number; top: number; bottom: number; width: number; up: boolean; maxH: number } | null
  >(null);

  const place = useCallback(() => {
    const el = btnRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const below = window.innerHeight - r.bottom;
    const above = r.top;
    const up = below < 300 && above > below;
    setBox({
      left: r.left, top: r.top, bottom: r.bottom, width: r.width, up,
      maxH: Math.max(220, (up ? above : below) - 14),
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    place();
    const onMove = () => place();
    window.addEventListener("scroll", onMove, true);
    window.addEventListener("resize", onMove);
    return () => {
      window.removeEventListener("scroll", onMove, true);
      window.removeEventListener("resize", onMove);
    };
  }, [open, place]);

  const close = () => { setOpen(false); setQ(""); };
  const ql = q.trim().toLowerCase();
  const match = (k: string) => !ql || k.toLowerCase().includes(ql) || label(k).toLowerCase().includes(ql);
  const suggSet = new Set(suggested);
  const suggF = suggested.filter(match);
  const allF = allFields.filter(k => !suggSet.has(k) && match(k));
  const CAP = 300;

  return (
    <>
      <button ref={btnRef} type="button" className="fp-trigger" onClick={() => setOpen(o => !o)}>
        <span className={value ? "" : "ph"}>{value ? label(value) : "— Choose Field —"}</span>
        <span className="car">▾</span>
      </button>
      {open && box && (
        <>
          <div style={{ position: "fixed", inset: 0, zIndex: 55 }} onClick={close} />
          <div className="fp-menu" style={{
            left: box.left, width: Math.max(box.width, 320), maxHeight: box.maxH,
            ...(box.up ? { bottom: window.innerHeight - box.top + 4 } : { top: box.bottom + 4 }),
          }}>
            <input autoFocus className="fp-search" placeholder="Search Fields…"
              value={q} onChange={e => setQ(e.target.value)} />
            <button className={`fp-opt${!value ? " on" : ""}`}
              onClick={() => { onPick(null); close(); }}>— Choose Field —</button>
            {suggF.length > 0 && <div className="fp-group">Suggested</div>}
            {suggF.map(k => (
              <button key={k} className={`fp-opt${k === value ? " on" : ""}`}
                onClick={() => { onPick(k); close(); }}>{label(k)}</button>
            ))}
            <div className="fp-group">All Kavachio Data-Model Field</div>
            {allF.slice(0, CAP).map(k => (
              <button key={k} className={`fp-opt${k === value ? " on" : ""}`}
                onClick={() => { onPick(k); close(); }}>{label(k)}</button>
            ))}
            {allF.length > CAP && (
              <div className="fp-empty">Showing first {CAP} — refine your search to narrow.</div>
            )}
            {suggF.length === 0 && allF.length === 0 && <div className="fp-empty">No matches.</div>}
          </div>
        </>
      )}
    </>
  );
}
