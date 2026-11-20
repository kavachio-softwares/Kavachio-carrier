// "Make this setup active again?" — a Bordereau Setup that was replaced,
// switched back on (backend setup_restore.py, task 51.1). Says which setup it
// replaces, asks why, and the step is recorded with the reason on
// Audit Logs → Versions & Restores. Files already processed are not changed.
import { useEffect, useState } from "react";
import { getRestorePreview, restoreSetup, type RestorePreview } from "../api/auditHistory";
import { showDone, showError } from "./ResultPopup";
import { Modal } from "./ui/Modal";
import { Button } from "./ui/Button";

export function RestoreSetupModal({ pipelineId, open, onClose, onDone }: {
  pipelineId: number; open: boolean; onClose: () => void; onDone: () => void;
}) {
  const [pre, setPre] = useState<RestorePreview | null>(null);
  const [why, setWhy] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    setPre(null); setWhy("");
    getRestorePreview(pipelineId).then(setPre).catch(e => { showError(e, "Could not check this setup"); onClose(); });
  }, [open, pipelineId]); // eslint-disable-line react-hooks/exhaustive-deps
  const replaced = pre?.replaces.map(x => x.name).join(", ");
  async function go() {
    setBusy(true);
    try { await restoreSetup(pipelineId, why.trim()); showDone("This setup is active again. The reason is kept in Audit Logs."); onDone(); }
    catch (e) { showError(e, "Could not make this setup active again"); }
    finally { setBusy(false); }
  }
  return (
    <Modal open={open} size="md" title="Make this setup active again?" onClose={() => !busy && onClose()}
      footer={<div className="flex items-center justify-end gap-2">
        <Button variant="secondary" disabled={busy} onClick={onClose}>Cancel</Button>
        <Button disabled={busy || !pre?.ready || !why.trim()} onClick={go}>{busy ? "Working…" : "Make active again"}</Button>
      </div>}>
      {!pre ? <div className="text-sm text-ink-muted">Checking…</div> : (
        <div className="text-sm space-y-3">
          <p>
            <strong>{pre.setup}</strong> will be used for new files again.
            {replaced ? <> The setup in use now — <strong>{replaced}</strong> — will be replaced.</> : " No other setup is in use for it now."}
            {" "}Files already processed are not changed.
          </p>
          {!pre.ready && <p className="text-[color:var(--p-crit,#dc2626)]">This setup is not complete, so it cannot be used: {pre.not_ready_reason}</p>}
          <label className="grid gap-1">
            <span>Why are you doing this?</span>
            <textarea rows={3} value={why} onChange={e => setWhy(e.target.value)}
              placeholder="e.g. The new setup maps premium to the wrong column."
              className="w-full rounded-md border border-border px-2 py-1.5" />
          </label>
        </div>)}
    </Modal>
  );
}
