/**
 * Approvals — the one gate in the platform.
 *
 * A contract a BROKER uploaded waits here for its carrier. A contract the
 * carrier uploaded is live on arrival and never appears. Nothing else in
 * Kavachio is ever approved, which is why this queue is small and specific
 * rather than a general "things to do" list.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, XCircle, Clock, FileText, Loader2 } from "lucide-react";
import {
  getApprovals, approveContract, rejectContract, getApprovalHistory,
  type PendingApproval, type ApprovalEvent,
} from "../api/hierarchy";
import { fmtStamp } from "../utils/date";
import Card from "../components/ui/Card";
import Button from "../components/ui/Button";
import Modal from "../components/ui/Modal";
import { PageBody, PageHeader } from "../components/Layout";

export default function Approvals() {
  const [rows, setRows] = useState<PendingApproval[] | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [rejecting, setRejecting] = useState<PendingApproval | null>(null);
  const [reason, setReason] = useState("");
  const [history, setHistory] = useState<{ row: PendingApproval; events: ApprovalEvent[] } | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(() => {
    getApprovals().then(setRows).catch(() => setRows([]));
  }, []);
  useEffect(load, [load]);

  async function approve(row: PendingApproval) {
    setBusy(row.contract_id);
    try {
      await approveContract(row.contract_id);
      setNote(`Approved. ${row.filename ?? "The contract"} is live — its checks run on the next file ${row.broker?.legal_name ?? "this broker"} sends.`);
      load();
    } finally { setBusy(null); }
  }

  async function reject() {
    if (!rejecting || !reason.trim()) return;
    setBusy(rejecting.contract_id);
    try {
      await rejectContract(rejecting.contract_id, reason.trim());
      setNote(`Sent back to ${rejecting.broker?.legal_name ?? "the broker"} with your reason.`);
      setRejecting(null); setReason("");
      load();
    } finally { setBusy(null); }
  }

  async function showHistory(row: PendingApproval) {
    const events = await getApprovalHistory(row.contract_id);
    setHistory({ row, events });
  }

  return (
    <>
      <PageHeader
        title="Approvals"
        subtitle="Contracts your brokers have uploaded, waiting on you. Anything you upload yourself is live straight away and never lands here."
      />
      <PageBody>
        {note && (
          <div className="rounded-md border border-success/30 bg-success/5 px-4 py-3 text-sm flex items-start gap-2">
            <CheckCircle2 size={16} className="text-success mt-0.5 shrink-0" />
            <span className="flex-1">{note}</span>
            <button className="text-ink-muted hover:text-ink" onClick={() => setNote(null)}>Dismiss</button>
          </div>
        )}

        {rows === null && (
          <div className="flex items-center gap-2 text-sm text-ink-muted">
            <Loader2 size={15} className="animate-spin" /> Loading…
          </div>
        )}

        {rows?.length === 0 && (
          <Card>
            <div className="py-10 text-center">
              <CheckCircle2 size={26} className="mx-auto text-success mb-3" />
              <p className="text-sm font-medium">Nothing is waiting on you.</p>
              <p className="text-sm text-ink-muted mt-1">
                When a broker uploads a contract, it appears here until you approve it.
              </p>
            </div>
          </Card>
        )}

        {rows?.map(row => (
          <Card key={row.contract_id}>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <FileText size={16} className="text-ink-muted shrink-0" />
                  <h3 className="font-semibold truncate">{row.filename ?? `Contract ${row.contract_id}`}</h3>
                </div>
                <dl className="mt-3 grid grid-cols-[auto,1fr] gap-x-4 gap-y-1.5 text-sm">
                  <dt className="text-ink-muted">Broker</dt>
                  <dd>
                    {row.broker
                      ? <Link className="text-navy hover:underline" to={`/brokers/${row.broker.id}`}>{row.broker.legal_name}</Link>
                      : <span className="text-ink-muted">—</span>}
                  </dd>
                  <dt className="text-ink-muted">Programme</dt>
                  <dd>{row.programme?.name ?? "—"}</dd>
                  <dt className="text-ink-muted">Term</dt>
                  <dd>{row.inception_dt && row.expiry_dt ? `${row.inception_dt} → ${row.expiry_dt}` : "—"}</dd>
                  <dt className="text-ink-muted">Sent by</dt>
                  <dd>
                    {row.submitted_by
                      ? <>{row.submitted_by.full_name} <span className="text-ink-muted">· {row.submitted_by.email}</span></>
                      : "—"}
                  </dd>
                  <dt className="text-ink-muted">Waiting since</dt>
                  <dd className="flex items-center gap-1.5">
                    <Clock size={13} className="text-warn" />
                    {row.submitted_at ? fmtStamp(row.submitted_at) : "—"}
                  </dd>
                </dl>
                <button className="mt-3 text-sm text-navy hover:underline" onClick={() => showHistory(row)}>
                  How it got here →
                </button>
              </div>

              <div className="flex gap-2 shrink-0">
                <Button variant="secondary" disabled={busy === row.contract_id}
                        onClick={() => { setRejecting(row); setReason(""); }}>
                  <XCircle size={15} /> Send it back
                </Button>
                <Button disabled={busy === row.contract_id} onClick={() => approve(row)}>
                  {busy === row.contract_id ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}
                  Approve
                </Button>
              </div>
            </div>
          </Card>
        ))}
      </PageBody>

      {/* A rejection without a reason is not a decision, it is a wall — the
          backend refuses one, so the button stays disabled until there is one. */}
      <Modal open={!!rejecting} onClose={() => setRejecting(null)} title="Send it back">
        <p className="text-sm text-ink-muted">
          {rejecting?.broker?.legal_name ?? "The broker"} sees this reason, so say what has to change.
          They can fix the contract and upload it again.
        </p>
        <textarea
          className="mt-3 w-full rounded-md border border-border px-3 py-2 text-sm"
          rows={4} autoFocus value={reason} onChange={e => setReason(e.target.value)}
          placeholder="e.g. The commission table on page 4 is missing."
        />
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setRejecting(null)}>Cancel</Button>
          <Button variant="danger" disabled={!reason.trim() || busy !== null} onClick={reject}>
            Send it back
          </Button>
        </div>
      </Modal>

      <Modal open={!!history} onClose={() => setHistory(null)} title="How it got here">
        <ol className="space-y-3">
          {history?.events.map((e, i) => (
            <li key={i} className="flex gap-3 text-sm">
              <span className="mt-1.5 h-2 w-2 rounded-full bg-navy shrink-0" />
              <div>
                <div className="font-medium capitalize">{e.action}</div>
                <div className="text-ink-muted">
                  {e.acted_by?.full_name ?? "—"}
                  {e.acted_at ? ` · ${fmtStamp(e.acted_at)}` : ""}
                </div>
                {e.note && <div className="mt-1 rounded bg-surface-2 px-2.5 py-1.5">{e.note}</div>}
              </div>
            </li>
          ))}
        </ol>
      </Modal>
    </>
  );
}
