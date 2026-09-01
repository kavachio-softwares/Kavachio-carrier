// The "invite is on its way" confirmation dialog — the green tick popup shown
// after Add Broker and Invite User. It was copy-pasted in both of those pages;
// it now lives here so every flow that mails an invite (first send AND resend)
// confirms it the SAME way, instead of some showing a popup and others a small
// inline banner.
//
// Styling is proto.css `.proto-modal.tenant-success` — unchanged, so the
// existing screens look exactly as they did.
export function InviteSentModal({
  title, message, email, emailLabel = "Invite sent to", note,
  doneLabel = "Done", onDone,
}: {
  /** Headline, e.g. "User created" / "Invite re-sent". */
  title: string;
  /** One line of context under the headline. */
  message: string;
  /** Recipient shown in the envelope row; omit to hide the row. */
  email?: string;
  emailLabel?: string;
  /** Small print under the envelope row. */
  note?: string;
  doneLabel?: string;
  onDone: () => void;
}) {
  return (
    <div className="proto-modal-overlay">
      <div className="proto-modal tenant-success" onClick={e => e.stopPropagation()}>
        <div className="ts-icon">
          <svg viewBox="0 0 56 56" fill="none" aria-hidden="true">
            <circle cx="28" cy="28" r="28" fill="var(--p-ok-soft)" />
            <path d="M18 28.5l6.5 6.5L38 21" stroke="var(--p-ok)" strokeWidth="3"
              strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
        <h3 className="ts-title">{title}</h3>
        <p className="ts-org">{message}</p>
        {email && (
          <div className="ts-row">
            <svg className="ts-row-ic" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <rect x="2.5" y="4.5" width="15" height="11" rx="2" stroke="currentColor" strokeWidth="1.4" />
              <path d="M3 5.5l7 5.5 7-5.5" stroke="currentColor" strokeWidth="1.4"
                strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span>{emailLabel} <b>{email}</b></span>
          </div>
        )}
        {note && <p className="ts-sub">{note}</p>}
        <button className="btn pri ts-done" onClick={onDone}>{doneLabel}</button>
      </div>
    </div>
  );
}
