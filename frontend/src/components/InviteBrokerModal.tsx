/**
 * Invite a broker without leaving the screen you are on.
 *
 * The same act as the Party screen's "Invite a party" page (AddUser with
 * ?for=broker) — one call that creates the broker organisation and invites its
 * first admin — but in a dialog, for flows where navigating away would break
 * the job in progress. Configure Program uses it: you are part-way through
 * putting brokers on a programme, and the one you need is not on the list.
 *
 * It reports back only the server's own message. The invite deliberately does
 * not say whether the address belonged to an existing broker (see
 * POST /brokers), so neither does this; the caller refreshes its list to see
 * who is there now.
 */
import { useEffect, useState } from "react";
import { Modal } from "./ui/Modal";
import { inviteBroker } from "../api/hierarchy";
import { addsCarrierUsers, useCarrierSeat } from "../hooks/useCarrierSeat";

// The kinds of organisation that can produce business — the same four the
// invite page offers. A broker is the usual one; the others occupy the same
// slot on a programme on the same terms.
const PARTY_TYPES: [string, string][] = [
  ["broker", "Broker"], ["mga", "MGA"], ["mgu", "MGU"], ["tpa", "TPA"],
];

const INPUT = "w-full rounded border border-border px-2.5 py-1.5 text-sm";
const LABEL = "mb-1 block text-xs font-medium text-ink-muted";

export function InviteBrokerModal({ open, onClose, onInvited, programId }: {
  open: boolean;
  onClose: () => void;
  /** The programme this was opened from, when it was opened from one. Sent
   *  with the invite so the broker and the programme are ONE ask — see
   *  inviteBroker. Omitted where there is no programme in view. */
  programId?: number;
  /** Called with the server's confirmation and the address it concerns.
   *  `pending` is true when this only ASKED — a carrier user's invite raises a
   *  request for their carrier admin and sends the broker nothing. `org` is
   *  what was typed, so the caller can name it rather than repeating the
   *  server's longer sentence. */
  onInvited: (message: string, email: string, pending?: boolean,
              org?: string) => void;
}) {
  const seat = useCarrierSeat();
  // The carrier admin's own invite goes out at once; everyone else's is asked
  // first. Drawn from the seat only so the dialog does not PROMISE an email it
  // is not going to send — the server decides, and reports back in `pending`.
  const needsApproval = seat !== null && !addsCarrierUsers(seat);
  const [orgName, setOrgName] = useState("");
  const [orgType, setOrgType] = useState("broker");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // A fresh form each time it opens — a second invite should not start from
  // the first one's details.
  useEffect(() => {
    if (!open) return;
    setOrgName(""); setOrgType("broker"); setFullName(""); setEmail("");
    setErr(null); setBusy(false);
  }, [open]);

  const canSend = orgName.trim() !== "" && fullName.trim() !== ""
    && email.includes("@") && !busy;

  async function send() {
    if (!canSend) return;
    setBusy(true); setErr(null);
    try {
      const r = await inviteBroker({
        legal_name: orgName.trim(),
        party_type: orgType,
        admin_name: fullName.trim(),
        admin_email: email.trim(),
        ...(programId != null ? { program_id: programId } : {}),
      });
      onInvited(r.message, email.trim(), r.pending ?? !r.invited,
                orgName.trim());
      onClose();
    } catch (e: any) {
      // `detail` is a string for simple refusals and an object for the ones
      // carrying a remedy — unpacked rather than rendered raw.
      const d = e?.response?.data?.detail;
      setErr((typeof d === "string" ? d : d?.message) ?? "Could not send the invitation.");
    } finally { setBusy(false); }
  }

  return (
    <Modal open={open} onClose={onClose}
      title={needsApproval ? "Ask to invite a broker" : "Invite a broker"} size="2xl"
      footer={<>
        <button type="button"
          className="rounded border border-border px-3 py-1.5 text-sm hover:bg-surface-2"
          onClick={onClose}>Cancel</button>
        <button type="button"
          className="rounded bg-navy px-3 py-1.5 text-sm font-medium text-white hover:bg-navy-dark disabled:opacity-50"
          onClick={send} disabled={!canSend}
          title={canSend ? undefined : "Fill in the organisation, name and email first"}>
          {busy ? "Sending…"
                : needsApproval ? "Send for approval" : "Send invite"}
        </button>
      </>}>
      <div className="space-y-5">
        {err && (
          <div className="rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">{err}</div>
        )}

        <section className="space-y-3">
          <h3 className="text-sm font-semibold">Broker organisation</h3>
          <div className="grid gap-3 sm:grid-cols-[1fr_9rem]">
            <div>
              <label className={LABEL}>Organisation name</label>
              <input className={INPUT} autoFocus value={orgName}
                placeholder="e.g. Marlowe Broking Ltd"
                onChange={e => setOrgName(e.target.value)} />
            </div>
            <div>
              <label className={LABEL}>Type</label>
              <select className={INPUT} value={orgType} onChange={e => setOrgType(e.target.value)}>
                {PARTY_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
          </div>
        </section>

        <section className="space-y-3">
          <h3 className="text-sm font-semibold">Their first admin</h3>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className={LABEL}>Full name</label>
              <input className={INPUT} value={fullName} placeholder="e.g. Priya Nair"
                onChange={e => setFullName(e.target.value)} />
            </div>
            <div>
              <label className={LABEL}>Email</label>
              <input className={INPUT} type="email" value={email}
                placeholder="name@company.com"
                onChange={e => setEmail(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); send(); } }} />
            </div>
          </div>
          <p className="text-xs text-ink-muted">
            {needsApproval
              ? "Nothing is sent to this address yet — your carrier admin "
                + "approves it first."
              : "The invitation and a password-setup link are sent to this address."}
          </p>
        </section>

        {/* Stated generally on purpose: which of the two cases this is, the
            server does not say, and the dialog must not guess.

            For a carrier user the whole paragraph is beside the point — none of
            it happens until their carrier admin has agreed — so they get the
            one fact that is true now instead. */}
        <p className="rounded border border-border bg-surface-2 px-3 py-2 text-xs leading-relaxed text-ink-muted">
          {needsApproval
            ? "This goes to your carrier admin first. Nothing at all is sent to "
              + "the broker until they approve it, so if it is turned down the "
              + "broker never knows it was asked."
            : "A broker new to the platform can be put on this programme straight "
              + "away. One who already works with another carrier keeps their login "
              + "and their own organisation name, and joins once they accept your "
              + "invitation."}
        </p>
      </div>
    </Modal>
  );
}

export default InviteBrokerModal;
