/**
 * Files Received → file panel: the submission this file belongs to.
 *
 * Version, where it stands, the "12 → 8 fixed → 4 remaining"
 * progress, and — the question a carrier actually asks — whether the broker
 * was told. Two carrier moves only: send the broker the result again, and
 * accept a file as it is when the deadline rule has left the decision to the
 * carrier. Answering an exception stays the broker's job.
 */
import { useCallback, useEffect, useState } from "react";
import { InfoTip } from "./InfoTip";
import { canAmendExceptions } from "../auth";
import {
  deliverAsItIs, notifyAgain, submissionForArrival, type SubmissionDoc,
} from "../api/submissions";

/** How a version came in, in the words Files Received uses. */
const CAME_BY: Record<string, string> = {
  upload: "Manual Upload", email: "Email", sftp: "SFTP", api: "API",
  secure_link: "Secure Link",
};

function when(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit" });
}

function toldLine(d: SubmissionDoc): { text: string; tone: "" | "warn" | "crit" } {
  const n = d.broker_notified;
  if (!n) return { text: "Not yet — the result is still being prepared.", tone: "" };
  const who = n.recipients.join(", ");
  if (n.failed) return { text: `Could not be reached (${who}). Try “Send Again”, or contact the broker.`, tone: "crit" };
  if (n.queued) return { text: `Sending to ${who}…`, tone: "" };
  if (n.skipped && !n.sent) return { text: `Not sent — broker emails are switched off or in test mode (${who}).`, tone: "warn" };
  return { text: `Emailed ${who} · ${when(n.at)}`, tone: "" };
}

export default function SubmissionPanel({ arrivalId, onChanged, onOpenVersion }: {
  arrivalId: number; onChanged?: () => void;
  /** Open another file version of this submission (Files Received). */
  onOpenVersion?: (arrivalId: number) => void;
}) {
  const [doc, setDoc] = useState<SubmissionDoc | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);

  const load = useCallback(() => {
    submissionForArrival(arrivalId).then(setDoc).catch(() => setDoc(null));
  }, [arrivalId]);
  useEffect(() => { setDoc(undefined); setErr(null); setConfirm(false); load(); }, [load]);

  if (doc === undefined || doc === null) return null;
  // Versions made from secure-link corrections before they were sent as files
  // have no file row of their own, so "latest" is the newest FILE.
  const latestFile = Math.max(0, ...doc.versions.filter((v) => v.is_file !== false).map((v) => v.version));
  const isLatestFile = doc.this_file_version >= latestFile;
  const told = toldLine(doc);
  // Carrier seats act here; a broker seat never sees Files Received anyway.
  const carrier = !canAmendExceptions();

  async function act(what: "deliver" | "notify") {
    if (!doc) return;
    setBusy(true); setErr(null);
    try {
      if (what === "deliver") await deliverAsItIs(doc.reference);
      else await notifyAgain(doc.reference);
      setConfirm(false);
      load();
      onChanged?.();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setErr(detail || "The action could not be completed. No changes were made.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="sub-h">
        Broker Follow-Up
        <InfoTip text={"The broker is told the result on the channel they used and by email, with a secure link "
          + "to resolve exceptions. Each correction is a new version, checked again. Only the broker can resolve an exception."} />
      </div>
      <div className="kv"><span className="k">Version</span>
        <span className="v">Version {doc.this_file_version}
          {doc.version && doc.version !== doc.this_file_version ? ` · latest is version ${doc.version}` : ""}</span></div>
      {/* Every version, oldest first. Files Received lists a submission once,
          as its latest file — an earlier file opens from here. */}
      {doc.versions.length > 1 && (
        <div className="kv" style={{ alignItems: "flex-start" }}><span className="k">History</span>
          <span className="v" style={{ display: "flex", flexWrap: "wrap", gap: 4, justifyContent: "flex-end" }}>
            {doc.versions.map((v) => {
              const here = v.is_file !== false && v.version === doc.this_file_version;
              const openable = !here && v.arrival_id != null && !!onOpenVersion;
              return (
                <button key={v.version} type="button" disabled={!openable}
                  className={`badge ${here ? "b-info" : "b-mut"}`}
                  style={{ border: 0, cursor: openable ? "pointer" : "default" }}
                  title={openable ? `Open version ${v.version}` : undefined}
                  onClick={() => { if (openable) onOpenVersion!(v.arrival_id!); }}>
                  v{v.version}{CAME_BY[v.source] ? ` · ${CAME_BY[v.source]}` : ""} · {v.status_text}
                </button>);
            })}
          </span></div>)}
      <div className="kv"><span className="k">Status</span>
        <span className="v">{doc.status_text}</span></div>
      {doc.progress_text && (
        <div className="kv"><span className="k">Progress</span>
          <span className="v">{doc.progress_text}</span></div>)}
      {doc.deadline && !doc.status.startsWith("delivered") && (
        <div className="kv"><span className="k">Broker Deadline</span>
          <span className="v">{when(doc.deadline)}</span></div>)}
      <div className="kv"><span className="k">Broker Notified</span>
        <span className="v" style={{ color: told.tone === "crit" ? "var(--p-crit)"
          : told.tone === "warn" ? "var(--p-warn)" : undefined }}>{told.text}</span></div>
      {doc.message && (
        <div className="note" style={{ marginTop: 8 }}>{doc.message}</div>)}
      {doc.status === "held_at_deadline" && (
        <div className="note warn" style={{ marginTop: 8 }}>
          The correction deadline passed with exceptions still open. Your programme rule keeps the
          file on hold — accept it as it is, or keep waiting for the broker.
        </div>)}
      {err && <div className="note crit" style={{ marginTop: 8 }}>{err}</div>}
      {/* Actions live on the LATEST file only — an older file's panel just
          says which version is current — and only when they can do something:
          Send Again while the broker still owes an answer, Accept As It Is once
          the deadline rule has left the decision to the carrier. */}
      {carrier && isLatestFile && (doc.can_notify || doc.can_deliver) && (
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          {doc.can_notify && (
          <button className="btn" disabled={busy} onClick={() => act("notify")}
            title="Email the broker the current result and a fresh secure link">Send Again</button>)}
          {doc.can_deliver && !confirm && (
            <button className="btn" disabled={busy} onClick={() => setConfirm(true)}>
              Accept As It Is</button>)}
          {doc.can_deliver && confirm && (<>
            <span style={{ fontSize: 12, alignSelf: "center" }}>
              Deliver with {doc.progress.remaining} open exception{doc.progress.remaining === 1 ? "" : "s"} recorded?</span>
            <button className="btn pri" disabled={busy} onClick={() => act("deliver")}>
              {busy ? "Delivering…" : "Yes, Deliver"}</button>
            <button className="btn" disabled={busy} onClick={() => setConfirm(false)}>Cancel</button>
          </>)}
        </div>)}
    </>
  );
}
