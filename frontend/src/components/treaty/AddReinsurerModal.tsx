/**
 * Add a reinsurer — a record the carrier keeps, not an organisation that joins.
 *
 * Reinsurers have no seat in Kavachio: no invitation is sent and nobody logs
 * in. What the carrier needs is a name to put on a panel and, optionally,
 * somebody there to send a treaty's private read-only link to — so the first
 * contact can be added in the same breath.
 *
 * Opened from the directory and from inside the treaty editor, so a reinsurer
 * missing from a panel can be added without leaving the treaty.
 */
import { useEffect, useState } from "react";
import { Modal } from "../ui/Modal";
import CountryOptions from "../CountryOptions";
import { currentMga } from "../../auth";
import { fieldErrors } from "../../api/contractRecord";
import { createReinsurer } from "../../api/treaty";

const AM_BEST = ["A++ (Superior)", "A+ (Superior)", "A (Excellent)",
                 "A- (Excellent)", "B++ (Good)", "B+ (Good)"];

export function AddReinsurerModal({ open, onClose, onCreated }: {
  open: boolean;
  onClose: () => void;
  onCreated: (r: { id: number; name: string }) => void;
}) {
  const [name, setName] = useState("");
  const [rating, setRating] = useState("");
  const [country, setCountry] = useState("");
  const [contactName, setContactName] = useState("");
  const [contactEmail, setContactEmail] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName(""); setRating(""); setCountry("");
    setContactName(""); setContactEmail(""); setErr("");
  }, [open]);

  const emailOk = !contactEmail.trim() || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(contactEmail.trim());
  const contactHalf = !!contactName.trim() !== !!contactEmail.trim();

  async function save() {
    setBusy(true); setErr("");
    try {
      const r = await createReinsurer(currentMga(), {
        legal_name: name.trim(),
        am_best_rating: rating || null,
        domicile_country: country || null,
        contact: contactName.trim()
          ? { full_name: contactName.trim(), email: contactEmail.trim() }
          : null,
      });
      onCreated({ id: r.id, name: r.legal_name });
      onClose();
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} size="md" title="Add a reinsurer"
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>Cancel</button>
          <button className="btn pri" type="button"
                  disabled={busy || !name.trim() || !emailOk || contactHalf}
                  onClick={save}>
            {busy ? "Adding…" : "Add reinsurer"}
          </button>
        </>
      }>
      <div className="proto">
        <p className="sub" style={{ marginTop: 0 }}>
          Reinsurers are not on Kavachio. This is your record of them — nobody
          is invited and nobody logs in. Add a contact if you want to share
          treaties with them through a private link.
        </p>
        {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}
        <div className="field">
          <label>Legal name<span style={{ color: "var(--p-crit-ink)" }}>*</span></label>
          <input value={name} autoFocus placeholder="e.g. Munich Re"
                 onChange={e => setName(e.target.value)} />
        </div>
        <div className="grid g-2" style={{ gap: 14 }}>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>AM Best rating <span className="muted" style={{ fontWeight: 500 }}>— optional</span></label>
            <select value={rating} onChange={e => setRating(e.target.value)}>
              <option value="">—</option>
              {AM_BEST.map(r => <option key={r} value={r}>{r}</option>)}
            </select>
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Country <span className="muted" style={{ fontWeight: 500 }}>— optional</span></label>
            <select value={country} onChange={e => setCountry(e.target.value)}>
              <option value="">—</option>
              <CountryOptions />
            </select>
          </div>
        </div>
        <div className="divider" />
        <div className="sub" style={{ marginBottom: 8 }}>
          First contact — optional. Their email is where a treaty link goes.
        </div>
        <div className="grid g-2" style={{ gap: 14 }}>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Name</label>
            <input value={contactName} onChange={e => setContactName(e.target.value)} />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Email</label>
            <input type="email" value={contactEmail}
                   onChange={e => setContactEmail(e.target.value)}
                   style={!emailOk ? { borderColor: "var(--p-crit)" } : undefined} />
          </div>
        </div>
        {contactHalf && (
          <div className="hint" style={{ color: "var(--p-warn-ink)", marginTop: 6, fontSize: 11.5 }}>
            A contact needs both a name and an email.
          </div>
        )}
      </div>
    </Modal>
  );
}
