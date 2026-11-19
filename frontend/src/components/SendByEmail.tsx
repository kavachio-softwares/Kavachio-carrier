// The "send by email" popup, shared by the Executive Report (38.2) and the
// Policies list (43.2): your company's logins to tick, any other address typed
// one per line, and the file's format when there is a choice. Each screen says
// what is being sent and makes the call; the popup only gathers who and how,
// and reports back who got it.
import { useEffect, useState, type ReactNode } from "react";
import { InfoTip } from "./InfoTip";
import { showDone, showError } from "./ResultPopup";
import { Modal } from "./ui/Modal";
import { plural } from "../utils/reportFormat";

/** A login that can be ticked. */
export interface SendLogin { user_id: number; email: string; name: string | null }

/** What the server says after sending. */
export interface SendOutcome {
  delivered: number;
  failed: number;
  recipients: { name: string | null; email: string; delivered: boolean }[];
}

export const who = (p: { name: string | null; email: string }) => (p.name ? `${p.name} (${p.email})` : p.email);

/** Typed addresses: one per line, or split by commas, spaces or semicolons. */
export const lines = (t: string) => t.split(/[\s,;]+/).map(x => x.trim()).filter(Boolean);

/** Logins to tick, and other addresses one per line. `noun` names what is
 *  sent, for the ⓘ ("the report", "the list"). */
export function Recipients({ logins, ticked, setTicked, emails, setEmails, noun = "report" }: {
  logins: SendLogin[]; ticked: number[]; setTicked: (v: number[]) => void;
  emails: string; setEmails: (v: string) => void; noun?: string;
}) {
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div>
        <div className="sub" style={{ marginBottom: 6 }}>Your company&rsquo;s logins</div>
        {logins.length === 0 ? <div className="sub">No logins with an email.</div> : logins.map(p => (
          <label key={p.user_id} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
            <input type="checkbox" checked={ticked.includes(p.user_id)}
              onChange={e => setTicked(e.target.checked ? [...ticked, p.user_id] : ticked.filter(x => x !== p.user_id))} />
            {who(p)}
          </label>
        ))}
      </div>
      <label style={{ display: "grid", gap: 6 }}>
        <span className="sub">Other email addresses <InfoTip text={`One per line. Anyone you add gets the ${noun} by email — they do not need a login.`} /></span>
        <textarea rows={4} value={emails} onChange={e => setEmails(e.target.value)}
          placeholder={"ceo@company.com\ncfo@company.com"}
          style={{ width: "100%", border: "1px solid var(--p-border-2)", borderRadius: 8, padding: 8, fontSize: 14 }} />
      </label>
    </div>
  );
}

export function FormatPick<F extends string>({ value, onChange, formats }: {
  value: F; onChange: (f: F) => void; formats: { key: F; label: string }[];
}) {
  return (
    <div>
      <div className="sub" style={{ marginBottom: 6 }}>Format</div>
      <span className="seg">{formats.map(f => (
        <button key={f.key} type="button" className={value === f.key ? "on" : ""} onClick={() => onChange(f.key)}>{f.label}</button>
      ))}</span>
    </div>
  );
}

/** "report" → "Report". */
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/**
 * Send something by email now.
 *  - `what`: the line at the top saying what is being sent.
 *  - `noun`: "report", "policy list" — for the result messages.
 *  - `ticked` / `emails`: who is filled in when the popup opens (none = every login).
 *  - `formats`: offered as a choice only when there is more than one.
 *  - `send`: makes the call with who was picked, and the format.
 */
export function SendByEmailModal<F extends string>({
  open, onClose, title, what, noun, logins, ticked: startTicked, emails: startEmails,
  formats, format: startFormat, send, onSent,
}: {
  open: boolean; onClose: () => void; title: string; what: ReactNode; noun: string;
  logins: SendLogin[]; ticked?: number[] | null; emails?: string[] | null;
  formats?: { key: F; label: string }[]; format?: F;
  send: (who: { user_ids: number[]; emails: string[]; file_format?: F }) => Promise<SendOutcome>;
  onSent: () => void;
}) {
  const [ticked, setTicked] = useState<number[]>([]);
  const [emails, setEmails] = useState("");
  const [format, setFormat] = useState<F | undefined>(startFormat);
  const [sending, setSending] = useState(false);
  useEffect(() => {
    if (!open) return;
    setTicked(startTicked ?? logins.map(p => p.user_id));
    setEmails((startEmails ?? []).join("\n"));
    setFormat(startFormat);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, startTicked, startEmails, startFormat, logins]);
  const count = ticked.length + lines(emails).length;

  async function go() {
    setSending(true);
    try {
      const r = await send({ user_ids: ticked, emails: lines(emails), file_format: format });
      const total = r.delivered + r.failed;
      if (r.failed === 0) showDone(`Sent to ${plural(r.delivered, "person", "people")}.`, `${cap(noun)} sent`);
      else showError(new Error(`Sent to ${r.delivered} of ${total}. Not delivered: `
        + r.recipients.filter(x => !x.delivered).map(x => x.email).join(", ")), `Not everyone got the ${noun}`);
      onSent();
    } catch (e) {
      showError(e, `Could not send the ${noun}`);
    } finally {
      setSending(false);
    }
  }

  return (
    <Modal open={open} size="md" title={title} onClose={() => { if (!sending) onClose(); }}
      footer={<div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button className="btn" onClick={onClose} disabled={sending}>Cancel</button>
        <button className="btn pri" onClick={go} disabled={sending || count === 0}>{sending ? "Sending…" : "Send"}</button>
      </div>}>
      <div style={{ display: "grid", gap: 16, fontSize: 14 }}>
        <div>{what}</div>
        <div><div className="sub" style={{ marginBottom: 6 }}>Send to</div>
          <Recipients logins={logins} ticked={ticked} setTicked={setTicked} emails={emails} setEmails={setEmails}
            noun={noun} /></div>
        {formats && formats.length > 1 && format !== undefined &&
          <FormatPick value={format} onChange={setFormat} formats={formats} />}
      </div>
    </Modal>
  );
}
