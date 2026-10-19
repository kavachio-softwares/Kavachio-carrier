import { useCallback, useEffect, useState } from "react";
import { Scale, X } from "lucide-react";
import {
  decideCase, enterDispute, getCase,
  type CaseInfo, type CasePayload,
} from "../api/calendar";
import { getBrokerCase, raiseBrokerDispute } from "../api/broker";

/**
 * MOPR 39.2 — the case on one missing file: where it stands, every move so
 * far, and what THIS viewer may do next. One component for both calendars:
 *
 *   carrier admin  accept or reject a dispute, waive (nil return), give more
 *                  time, or enter a dispute for the broker
 *   broker         raise a dispute (once — after a rejection only the carrier
 *                  can reopen it)
 *
 * What may be done comes from the server (`can`), which enforces the same
 * rules — the screen never decides them itself.
 */

const fmt = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const d = new Date(`${iso.slice(0, 10)}T00:00:00`);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
};
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** The case as a badge — the same words and colours on every screen. */
export function caseBadge(c: CaseInfo | null | undefined): { label: string; cls: string } | null {
  if (!c?.state) return null;
  switch (c.state) {
    case "overdue":    return { label: "Overdue", cls: "b-crit" };
    case "delinquent": return { label: "Delinquent", cls: "b-crit" };
    case "in_dispute": return { label: "In Dispute", cls: "b-warn" };
    case "extended":   return { label: `More time to ${fmt(c.until)}`, cls: "b-info" };
    case "resolved":   return { label: `Resolved · ${c.resolution_label ?? ""}`.replace(/ · $/, ""),
                                cls: "b-ok" };
    default:           return { label: String(c.state), cls: "b-mut" };
  }
}

type Action = "dispute" | "accept" | "reject" | "waive" | "extend" | "reopen";

const FILE_WORDS: Record<string, string> = {
  scheduled: "Not due yet", due_soon: "Due soon", due_today: "Due today",
  overdue: "Overdue", never_arrived: "Never arrived",
  on_time: "Arrived on time", received_late: "Arrived late",
};
// The broker SENDS the file, so its calendar says "Never sent" / "Sent late" —
// the panel says the same, not the carrier's "arrived".
const BROKER_FILE_WORDS: Record<string, string> = {
  ...FILE_WORDS,
  never_arrived: "Never sent", on_time: "Sent on time", received_late: "Sent late",
};

/** A missing file the carrier said is no longer owed — a dispute accepted, or
 *  the period waived — in words, e.g. "accepted (no policies sold)". Null otherwise. */
function settledWords(c: CaseInfo | null | undefined): string | null {
  if (c?.state !== "resolved") return null;
  if (c.resolution === "waived") return "closed by carrier";
  if (c.resolution === "accepted") {
    return c.dispute_reason === "nil_return" ? "accepted (no policies sold)" : "dispute accepted";
  }
  return null;
}

export function CaseModal({ expectedId, side, onClose, onChanged }: {
  expectedId: number;
  side: "carrier" | "broker";
  onClose: () => void;
  /** Called after a change is saved, so the calendar behind can reload. */
  onChanged?: () => void;
}) {
  const [data, setData] = useState<CasePayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState<Action | null>(null);
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [until, setUntil] = useState("");

  const load = useCallback(async () => {
    try {
      setData(side === "broker" ? await getBrokerCase(expectedId) : await getCase(expectedId));
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not load this case.");
    }
  }, [expectedId, side]);
  useEffect(() => { load(); }, [load]);

  const can = data?.can ?? {};
  const options: { key: Action; label: string; hint: string }[] = side === "broker"
    ? (can.raise_dispute ? [{ key: "dispute", label: "Raise a dispute",
        hint: "Say why this file should not count as late. The carrier decides; nothing more "
          + "happens to it until they do. You can raise one dispute per file." }] : [])
    : ([
      // Only on a case the carrier closed itself (accepted, or no file needed).
      can.reopen && { key: "reopen", label: "Reopen this case",
        hint: "Use this if the decision was wrong, for example policies were sold after "
          + "all. The file is owed again and reminders restart." },
      can.accept && { key: "accept", label: "Accept the dispute",
        hint: "The case is resolved — nothing more is owed or chased for this period." },
      can.reject && { key: "reject", label: "Reject the dispute",
        hint: "Back to Delinquent (or Overdue), and the broker cannot dispute it again." },
      can.waive && { key: "waive", label: "No file needed for this period",
        hint: "Choose this if the broker sold no policies during this period. No file is "
          + "needed, and reminders will stop. The calendar will still show that no file "
          + "was received." },
      can.extend && { key: "extend", label: "Give more time",
        hint: "No reminders until the new date. If it passes with nothing in, it is Overdue again." },
      can.raise_dispute && { key: "dispute", label: "Enter a dispute for the broker",
        hint: "When the broker told you by phone or email instead of using its calendar." },
    ].filter(Boolean) as { key: Action; label: string; hint: string }[]);

  const today = new Date().toISOString().slice(0, 10);
  const noteRequired = action !== null && (action !== "dispute" || reason === "other");
  const ready = action !== null
    && (action !== "dispute" || reason !== "")
    && (!noteRequired || note.trim() !== "")
    && (action !== "extend" || (until !== "" && until > today));

  async function submit() {
    if (!action || !data) return;
    setBusy(true); setErr(null); setMsg(null);
    try {
      const next = action === "dispute"
        ? (side === "broker"
          ? await raiseBrokerDispute(expectedId, reason, note.trim())
          : await enterDispute(expectedId, reason, note.trim()))
        : await decideCase(expectedId, action, note.trim(), action === "extend" ? until : null);
      setData(next);
      setMsg(action === "dispute" ? "Dispute recorded. The carrier decides next."
        : action === "accept" ? "Dispute accepted — the case is resolved."
        : action === "reject" ? "Dispute rejected — the broker has been told."
        : action === "waive" ? "Marked as no file needed — the case is resolved."
        : action === "reopen" ? "Case reopened — the broker has been told."
        : `More time given until ${fmt(until)}.`);
      setAction(null); setReason(""); setNote(""); setUntil("");
      onChanged?.();
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Could not save that.");
    } finally {
      setBusy(false);
    }
  }

  const c = data?.case;
  const badge = caseBadge(c);
  const fileWords = side === "broker" ? BROKER_FILE_WORDS : FILE_WORDS;
  const settled = settledWords(c);
  // Why the broker can no longer dispute: the carrier rejected its dispute, or
  // reopened a case it had closed. The latest of the two is the one to say.
  const lockedBy = [...(data?.history ?? [])].reverse()
    .find(h => h.kind === "dispute_rejected" || h.kind === "case_reopened")?.kind;
  // What the viewer is waiting for, when there is nothing for them to do.
  const waitingWords = !c?.state ? null
    : c.state === "in_dispute"
      ? (side === "broker" ? "Waiting for the carrier's decision on your dispute."
        : "Waiting for your decision.")
    : c.state === "extended" ? `More time was given until ${fmt(c.until)}.`
    : c.dispute_locked && side === "broker"
      ? (lockedBy === "case_reopened"
        ? "The carrier reopened this case — please send the file."
        : "The carrier rejected your dispute — please send the file.")
    : null;

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(16,20,28,.45)",
      display: "flex", alignItems: "center", justifyContent: "center",
      padding: 24, zIndex: 70 }}
      onClick={onClose}>
      <div className="card" style={{ width: "min(640px, 100%)", maxHeight: "88vh", margin: 0,
        display: "flex", flexDirection: "column", overflow: "hidden", borderRadius: 14,
        boxShadow: "0 24px 48px -12px rgb(16 20 28 / .28)" }}
        role="dialog" aria-modal="true" onClick={e => e.stopPropagation()}>

        {/* ---- header -------------------------------------------------- */}
        <div style={{ display: "flex", alignItems: "center", gap: 12,
          padding: "16px 20px", borderBottom: "1px solid var(--p-border)" }}>
          <span style={{ width: 36, height: 36, borderRadius: 10, flex: "none",
            background: "var(--p-primary-soft)", color: "var(--p-primary)",
            display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Scale size={16} />
          </span>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: "var(--p-ink)" }}>
              {data ? `${data.program_name} · ${data.period}` : "Late file"}
            </div>
            <div style={{ fontSize: 12.5, color: "var(--p-muted)", marginTop: 2 }}>
              {data
                ? `${side === "broker" ? data.carrier_name : data.broker_name ?? "Broker"}`
                  + ` · due ${fmt(data.due_date)}`
                : "Loading…"}
            </div>
          </div>
          <button type="button" onClick={onClose} aria-label="Close"
            style={{ width: 32, height: 32, borderRadius: 8, flex: "none",
              border: "1px solid var(--p-border)", background: "var(--p-surface)",
              color: "var(--p-muted)", cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center" }}>
            <X size={15} />
          </button>
        </div>

        {/* ---- body ---------------------------------------------------- */}
        <div style={{ padding: "18px 20px", overflowY: "auto", flex: 1 }}>
          {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
          {msg && <div className="note" style={{ marginBottom: 14 }}>{msg}</div>}

          {data && (
            <>
              {/* Where it stands: the file (39.1) and the case (39.2). */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div className="tile" style={{ padding: "12px 14px" }}>
                  <div className="k">The file</div>
                  {settled && c ? (
                    <>
                      {/* Settled: nothing more is owed, so say that first. The
                          missing file is still a fact — kept underneath, with
                          the count stopped on the day it was settled. */}
                      <div style={{ marginTop: 6, fontWeight: 600 }}>
                        {c.resolution === "accepted" && c.dispute_reason !== "nil_return"
                          ? "Nothing more owed" : "No file needed"}
                        <span className="muted" style={{ fontWeight: 400 }}>
                          {" "}· {settled} {fmt(c.since)}</span>
                      </div>
                      <div className="sub" style={{ marginTop: 3 }}>
                        {fileWords[data.status] ?? data.status}
                        {data.days_over != null
                          && ` · ${plural(data.days_over, "day")} over when it closed`}
                      </div>
                    </>
                  ) : (
                    <div style={{ marginTop: 6, fontWeight: 600 }}>
                      {fileWords[data.status] ?? data.status}
                      {data.days_over != null && (
                        <span className="muted" style={{ fontWeight: 400 }}>
                          {" "}· {plural(data.days_over, "day")} over</span>
                      )}
                    </div>
                  )}
                </div>
                <div className="tile" style={{ padding: "12px 14px" }}>
                  <div className="k">The case</div>
                  <div style={{ marginTop: 6 }}>
                    {badge
                      ? <span className={`badge ${badge.cls}`}><span className="d" />{badge.label}</span>
                      : <span className="muted">No case — not late</span>}
                    {c?.since && c.state !== "resolved" && (
                      <span className="muted" style={{ fontSize: 12 }}> since {fmt(c.since)}</span>
                    )}
                  </div>
                </div>
              </div>

              {c?.dispute_reason && (
                <div className="note" style={{ marginTop: 12 }}>
                  <b>Dispute{c.dispute_by_carrier ? " (entered by the carrier)" : ""}:</b>{" "}
                  {c.dispute_reason_label}
                  {c.dispute_note && <> — “{c.dispute_note}”</>}
                  {c.dispute_raised_on && (
                    <span className="muted"> · raised {fmt(c.dispute_raised_on)}</span>
                  )}
                  {c.dispute_locked && (
                    <div className="sub" style={{ marginTop: 4 }}>
                      A dispute on this file was rejected — the broker cannot raise another.
                    </div>
                  )}
                </div>
              )}

              <div className="hint" style={{ marginTop: 10 }}>
                A missing file becomes <b>Delinquent</b> {plural(data.delinquent_after_days, "day")} after
                its due date. A dispute pauses everything until the carrier decides.
              </div>

              {/* ---- what this viewer may do ---------------------------- */}
              {options.length > 0 ? (
                <div style={{ marginTop: 16 }}>
                  <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 8 }}>
                    {side === "broker" ? "Is this mark wrong?" : "Decide"}
                  </div>
                  <div style={{ display: "grid", gap: 6 }}>
                    {options.map(o => (
                      <label key={o.key} style={{ display: "flex", gap: 10, alignItems: "flex-start",
                        padding: "8px 10px", borderRadius: 8, cursor: "pointer",
                        border: `1px solid ${action === o.key ? "var(--p-primary)" : "var(--p-border)"}`,
                        background: action === o.key ? "var(--p-primary-soft)" : "var(--p-surface)" }}>
                        <input type="radio" name="case-action" checked={action === o.key}
                          onChange={() => { setAction(o.key); setErr(null); }}
                          style={{ marginTop: 3 }} />
                        <span>
                          <span style={{ fontWeight: 600 }}>{o.label}</span>
                          <span className="sub" style={{ display: "block" }}>{o.hint}</span>
                        </span>
                      </label>
                    ))}
                  </div>

                  {action === "dispute" && (
                    <div className="field" style={{ margin: "14px 0 0" }}>
                      <label>Reason</label>
                      <select className="fbar-select" value={reason}
                        onChange={e => setReason(e.target.value)}>
                        <option value="">— pick a reason —</option>
                        {data.reasons.map(r => <option key={r.key} value={r.key}>{r.label}</option>)}
                      </select>
                    </div>
                  )}
                  {action === "extend" && (
                    <div className="field" style={{ margin: "14px 0 0" }}>
                      <label>New date</label>
                      <input type="date" className="fbar-date" value={until} min={today}
                        onChange={e => setUntil(e.target.value)} />
                    </div>
                  )}
                  {action && (
                    <div className="field" style={{ margin: "14px 0 0" }}>
                      <label>
                        Note{" "}
                        {!noteRequired && (
                          <span style={{ fontWeight: 400, color: "var(--p-faint)" }}>(optional)</span>
                        )}
                      </label>
                      <textarea value={note} onChange={e => setNote(e.target.value)} rows={2}
                        style={{ resize: "vertical" }}
                        placeholder={action === "dispute"
                          ? "e.g. no policies were sold in this period"
                          : "e.g. confirmed by phone — the broker is told this"} />
                      <div className="hint">
                        {side === "carrier" && action !== "dispute"
                          ? "Required — the broker is emailed your decision with this note."
                          : "Kept with the case, so the reason is readable later."}
                      </div>
                    </div>
                  )}
                </div>
              ) : c?.state === "resolved" ? null : (
                // A closed case needs no line here — the badge above already
                // says how it closed, and repeating it read as a duplicate.
                <div className="note" style={{ marginTop: 16 }}>
                  {waitingWords
                    ?? (side === "carrier" && c?.state
                      ? "Only the carrier admin can decide this case."
                      : "Nothing to do on this file.")}
                </div>
              )}

              {/* No history list here — the panel is for deciding. Every move
                  is still recorded (submission_case_event, and `history` in
                  this payload) for an audit or report view to show. */}
            </>
          )}
        </div>

        {/* ---- footer -------------------------------------------------- */}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10,
          padding: "14px 20px", borderTop: "1px solid var(--p-border)",
          background: "var(--p-surface-2)" }}>
          <button className="btn" onClick={onClose} disabled={busy}>Close</button>
          {options.length > 0 && (
            <button className="btn pri" onClick={submit} disabled={busy || !ready}>
              {busy ? "Saving…"
                : action === "dispute" ? "Raise the dispute"
                : action === "accept" ? "Accept"
                : action === "reject" ? "Reject"
                : action === "waive" ? "No file needed"
                : action === "extend" ? "Give more time"
                : action === "reopen" ? "Reopen case"
                : "Choose an action"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
