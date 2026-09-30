/**
 * Broker onboarding requests — the carrier admin's queue.
 *
 * A carrier user wanting to bring a broker on board no longer does it: the
 * server asks their carrier admin first, and nothing is created and no email is
 * sent until this screen answers. A broker who is turned down never learns they
 * were considered, because there was never anything to learn about.
 *
 * ONE SCREEN, TWO READERS, as with Bordereau Setups:
 *
 *   carrier admin   every request at the carrier, with Approve and Turn down.
 *   carrier user    their own, so the reason a request was refused reaches
 *                   them — they will not be on the programme screen they
 *                   raised it from when the answer comes. They can withdraw
 *                   one nobody has answered.
 *
 * The seat is not what decides what to show: the SERVER already scoped the
 * list, and the buttons are drawn from the seat only so a carrier user is not
 * offered an Approve that would 403. The API remains the authority.
 *
 * APPROVING DOES NOT PUT THE PROGRAMME LIVE. It settles who the carrier works
 * with and sends the invitation; what the broker may send is still the
 * Bordereau Setup approval's question, and that gate is untouched. The copy
 * here says so, because two approvals in a row is exactly the sort of thing a
 * reader assumes is one.
 */
import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Handshake, Mail, ShieldCheck } from "lucide-react";
import {
  approveBrokerRequest, listBrokerRequests, rejectBrokerRequest,
  withdrawBrokerRequest,
  type BrokerOnboardingRequest, type BrokerRequestStatus,
} from "../api/brokerOnboarding";
import { Modal } from "../components/ui/Modal";
import { InfoTip } from "../components/InfoTip";
import { OrgAvatar } from "../components/ui/OrgAvatar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useCarrierSeat, addsCarrierUsers } from "../hooks/useCarrierSeat";
import { fmtStamp } from "../utils/date";

// Ten a table, as everywhere else. Two tables on this screen, so a screenful
// is twenty at its fullest — and the waiting one is nearly always short.
const PAGE_SIZE = 10;

// Fixed application states — a status column value, not tenant data.
const STATUS: Record<BrokerRequestStatus, { label: string; cls: string }> = {
  pending: { label: "Waiting on you", cls: "b-info" },
  approved: { label: "Approved", cls: "b-ok" },
  // Turned down, not "sent back": nothing was created, so there is nothing to
  // send back. A new request starts the ask again.
  rejected: { label: "Turned down", cls: "b-warn" },
  withdrawn: { label: "Withdrawn", cls: "b-mut" },
};

// The next step once a request is answered — there is nothing left to decide,
// but there is somewhere to go. Approved: the broker, now in your directory.
// Turned down or withdrawn: where a broker is added, to ask again.
function answeredAction(r: BrokerOnboardingRequest) {
  if (r.status === "approved") {
    return r.broker_party_id
      ? <Link className="linkish" to={`/brokers/${r.broker_party_id}`}>View broker →</Link>
      : <span className="faint">Invitation sent</span>;
  }
  return (
    <Link className="linkish"
      to={r.program_id ? `/programs/${r.program_id}/brokers` : "/brokers"}
      title="Nothing was created. Add the broker again to raise a new request.">
      Ask again →</Link>
  );
}

const person = (p: BrokerOnboardingRequest["requested_by"]) =>
  (p?.full_name || "").trim() || p?.email || "—";

export default function BrokerOnboardingRequests() {
  const seat = useCarrierSeat();
  const isAdmin = addsCarrierUsers(seat);

  const [busy, setBusy] = useState<number | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // The request being turned down, and the reason being typed for it.
  const [turningDown, setTurningDown] = useState<BrokerOnboardingRequest | null>(null);
  const [reason, setReason] = useState("");

  // TWO LISTS, PAGED SEPARATELY, and that is why the server grew an
  // `answered` filter. Waiting sits above answered because the waiting ones
  // are the only ones anybody came here to act on — and a single paged list
  // would put page one's answered rows above page two's waiting ones, which
  // is the wrong way round on the screen whose whole job is "what is waiting
  // on me". The answered half is also the half that grows for ever; the
  // waiting half empties as it is worked.
  //
  // Both fetchers report their own failure rather than letting the hook
  // swallow it: an empty table reads as "nothing waiting", which is a very
  // different thing from "we could not ask".
  //
  // A slot each, not one shared message. The two requests are in flight
  // together, so a single `err` would let the one that SUCCEEDED clear the
  // one that failed, purely on which answer came back last — and the reader
  // would be shown an empty table with nothing said about it.
  const [loadErr, setLoadErr] = useState<{ waiting?: string; answered?: string }>({});
  const fetchWaiting = useCallback((page: number, pageSize: number) =>
    listBrokerRequests({ status: "pending", page, page_size: pageSize })
      .then(r => { setLoadErr(e => ({ ...e, waiting: undefined })); return r; })
      .catch(e => {
        setLoadErr(x => ({ ...x, waiting: "Could not load what is waiting." }));
        throw e;
      }), []);
  const fetchAnswered = useCallback((page: number, pageSize: number) =>
    listBrokerRequests({ status: "answered", page, page_size: pageSize })
      .then(r => { setLoadErr(e => ({ ...e, answered: undefined })); return r; })
      .catch(e => {
        setLoadErr(x => ({ ...x, answered: "Could not load the answered requests." }));
        throw e;
      }), []);

  const waiting = useServerList<BrokerOnboardingRequest>(
    fetchWaiting, "pending", PAGE_SIZE);
  const answered = useServerList<BrokerOnboardingRequest>(
    fetchAnswered, "answered", PAGE_SIZE);

  // A decision moves a row from one table to the other, so both are asked
  // again — refreshing only the one that was acted on would leave the answer
  // nowhere to be seen.
  //
  // And if that row was the last one on the waiting page being read, the page
  // it was on has just stopped existing. Stepping back one is what keeps the
  // pager honest; reloading in place would draw an empty table under a count
  // that is not zero, which reads as the queue having broken.
  const { items: waitingItems, page: waitingPage,
          setPage: setWaitingPage, reload: reloadWaiting } = waiting;
  const { reload: reloadAnswered } = answered;
  const load = useCallback(() => {
    if (waitingItems.length <= 1 && waitingPage > 1) setWaitingPage(waitingPage - 1);
    else reloadWaiting();
    reloadAnswered();
  }, [waitingItems.length, waitingPage, setWaitingPage, reloadWaiting, reloadAnswered]);

  const firstLoad = waiting.loading && answered.loading
    && waiting.total === 0 && answered.total === 0;
  // "Nothing waiting" is a CLAIM about the queue, so it is only made when the
  // queue actually answered. A failed load leaves both totals at nought too,
  // and saying it then would be the screen inventing good news.
  const nothingAtAll = !waiting.loading && !answered.loading
    && waiting.total === 0 && answered.total === 0
    && !loadErr.waiting && !loadErr.answered;

  function fail(e: any, fallback: string) {
    const d = e?.response?.data?.detail;
    setErr((typeof d === "string" ? d : d?.message) ?? fallback);
  }

  async function approve(r: BrokerOnboardingRequest) {
    setBusy(r.id); setErr(null); setNote(null);
    try {
      await approveBrokerRequest(r.id);
      setNote(r.kind === "invite"
        ? `${r.broker_name} is on board — the invitation has gone to ${r.admin_email}.`
        : `${r.broker_name} was put on ${r.programme}.`);
      load();
    } catch (e) {
      fail(e, "Could not approve that request.");
    } finally { setBusy(null); }
  }

  async function turnDown() {
    const r = turningDown;
    if (!r || !reason.trim()) return;
    setBusy(r.id); setErr(null); setNote(null);
    try {
      await rejectBrokerRequest(r.id, reason.trim());
      setNote(`${r.broker_name} was turned down. `
        + `${person(r.requested_by)} has been told why; nothing was sent to the broker.`);
      setTurningDown(null); setReason("");
      load();
    } catch (e) {
      fail(e, "Could not turn that request down.");
    } finally { setBusy(null); }
  }

  async function withdraw(r: BrokerOnboardingRequest) {
    setBusy(r.id); setErr(null); setNote(null);
    try {
      const res = await withdrawBrokerRequest(r.id);
      setNote(res.message);
      load();
    } catch (e) {
      fail(e, "Could not withdraw that request.");
    } finally { setBusy(null); }
  }

  function row(r: BrokerOnboardingRequest) {
    const look = STATUS[r.status] ?? STATUS.pending;
    const mine = !isAdmin;
    return (
      <tr key={r.id}>
        <td>
          <div className="party-org">
            <OrgAvatar name={r.broker_name || "?"} />
            <div style={{ minWidth: 0 }}>
              {/* A broker being ASKED about has no page to open — for an
                  invitation there is no organisation yet, and for one out of
                  the directory the page would not show the request. The name
                  is a fact, not a link. */}
              <div className="party-nm">{r.broker_name || "—"}</div>
              <div className="sub">
                {r.kind === "invite" ? (
                  <><Mail size={12} style={{ verticalAlign: -2 }} />{" "}
                    {r.admin_name ? `${r.admin_name} · ` : ""}{r.admin_email}</>
                ) : (
                  <><Handshake size={12} style={{ verticalAlign: -2 }} />{" "}
                    Already works with you · nothing will be sent</>
                )}
              </div>
            </div>
          </div>
        </td>

        <td>
          {r.programme
            ? (r.program_id
                ? <Link to={`/programs/${r.program_id}/brokers`}
                        className="party-chip">{r.programme}</Link>
                : r.programme)
            : <span className="faint" title={"They were asked into your directory "
                + "rather than onto a programme. Put them on one afterwards."}>
                No programme yet
              </span>}
        </td>

        <td>
          <div>{person(r.requested_by)}</div>
          <div className="sub">{fmtStamp(r.requested_at)}</div>
        </td>

        <td>
          <span className={`badge ${look.cls}`}><span className="d" />{look.label}</span>
          {r.status !== "pending" && r.decided_by && (
            <div className="sub">
              {person(r.decided_by)} · {fmtStamp(r.decided_at)}
            </div>
          )}
          {/* The reason stands on the row for as long as it is kept, not only
              in the email — the person who asked comes back here to read it. */}
          {r.reason && <div className="sub" style={{ color: "var(--p-warn)" }}>
            “{r.reason}”
          </div>}
        </td>

        <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
          {r.status !== "pending" ? answeredAction(r)
            : isAdmin ? (
              <>
                <button className="btn pri sm" disabled={busy === r.id}
                  onClick={() => approve(r)}>
                  {busy === r.id ? "…" : "Approve"}
                </button>{" "}
                <button className="btn sm" disabled={busy === r.id}
                  onClick={() => { setTurningDown(r); setReason(""); }}>
                  Turn down
                </button>
              </>
            ) : mine ? (
              <button className="btn sm" disabled={busy === r.id}
                onClick={() => withdraw(r)}>
                Withdraw
              </button>
            ) : <span className="faint">—</span>}
        </td>
      </tr>
    );
  }

  const table = (list: BrokerOnboardingRequest[]) => (
    <div className="tbl-wrap">
      <table>
        <thead>
          <tr>
            <th>Broker</th>
            <th>Programme</th>
            <th>Asked by</th>
            <th>Status</th>
            <th style={{ width: 170, textAlign: "right" }}>Actions</th>
          </tr>
        </thead>
        <tbody>{list.map(row)}</tbody>
      </table>
    </div>
  );

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              Broker onboarding
              <InfoTip text={isAdmin
                ? "Brokers your carrier users want to bring on. Nothing has been "
                  + "sent to any of them — they hear from Kavachio only once you "
                  + "approve. Approving sends the invitation; what they may send "
                  + "you is still settled by the bordereau setup approval."
                : "Brokers you asked to bring on, and what the carrier "
                  + "decided. Nothing reaches a broker until they approve it."} />
            </h2>
            <p>
              {isAdmin
                ? ""
                : "What you asked for, and what the carrier said."}
            </p>
          </div>
        </div>

        {note && <div className="note ok" style={{ marginBottom: 16 }}>{note}</div>}
        {err && <div className="note warn" style={{ marginBottom: 16 }}>{err}</div>}
        {(loadErr.waiting || loadErr.answered) && (
          <div className="note warn" style={{ marginBottom: 16 }}>
            {[loadErr.waiting, loadErr.answered].filter(Boolean).join(" ")}
          </div>
        )}

        {firstLoad ? (
          <div className="card"><div className="empty">Loading…</div></div>
        ) : nothingAtAll ? (
          <div className="card">
            <div className="empty">
              <ShieldCheck size={22} style={{ margin: "0 auto 10px", display: "block" }} />
              <b style={{ color: "var(--p-ink)" }}>Nothing waiting</b>
              <div style={{ maxWidth: 460, margin: "4px auto 0" }}>
                {isAdmin
                  ? "When one of your carrier users wants to bring a broker on "
                    + "board, it will wait here for you. They will not have been "
                    + "sent anything."
                  : "Add a broker to one of your programmes and it will appear "
                    + "here while the carrier looks at it."}
              </div>
            </div>
          </div>
        ) : (
          <>
            {waiting.total > 0 && (
              <div className="card" style={{ marginBottom: 16 }}>
                <div className="card-h">
                  <b>{isAdmin ? "Waiting on you" : "Waiting on the carrier"}</b>
                  {/* The WHOLE queue's count, from the server — not
                      `items.length`, which would say 10 for ever once there
                      were eleven. */}
                  <div className="right"><span className="sub">{waiting.total}</span></div>
                </div>
                {table(waiting.items)}
                <Pagination page={waiting.page} pageCount={waiting.pageCount}
                  pageSize={PAGE_SIZE} totalItems={waiting.total}
                  onPageChange={waiting.setPage} noun="waiting" />
              </div>
            )}
            {answered.total > 0 && (
              <div className="card">
                <div className="card-h">
                  <b>Answered</b>
                  <div className="right"><span className="sub">{answered.total}</span></div>
                </div>
                {table(answered.items)}
                <Pagination page={answered.page} pageCount={answered.pageCount}
                  pageSize={PAGE_SIZE} totalItems={answered.total}
                  onPageChange={answered.setPage} noun="answered" />
              </div>
            )}
          </>
        )}
      </div>

      {/* Turning one down needs a reason, and the dialog exists to collect it.
          A request refused with nothing said about it is one its author cannot
          act on — they would have to come and ask, which is the conversation
          this saves. */}
      <Modal open={turningDown !== null} size="md"
        onClose={() => { setTurningDown(null); setReason(""); }}
        title={`Turn down ${turningDown?.broker_name ?? "this broker"}?`}
        footer={<>
          <button type="button"
            className="rounded border border-border px-3 py-1.5 text-sm hover:bg-surface-2"
            onClick={() => { setTurningDown(null); setReason(""); }}>Cancel</button>
          <button type="button"
            className="rounded bg-navy px-3 py-1.5 text-sm font-medium text-white hover:bg-navy-dark disabled:opacity-50"
            disabled={!reason.trim() || busy !== null}
            title={reason.trim() ? undefined : "Give a reason first"}
            onClick={turnDown}>
            {busy !== null ? "Sending…" : "Turn it down"}
          </button>
        </>}>
        <div className="space-y-3">
          <p className="text-sm">
            Nothing has been sent to them, so nobody outside your organisation
            knows this was asked.
          </p>
          <label className="mb-1 block text-xs font-medium text-ink-muted">
            Why — {person(turningDown?.requested_by ?? null)} will read this
          </label>
          <textarea
            className="w-full rounded border border-border px-2.5 py-1.5 text-sm"
            rows={3} autoFocus value={reason}
            placeholder="e.g. We already produce this class through Marlowe."
            onChange={e => setReason(e.target.value)} />
        </div>
      </Modal>
    </div>
  );
}
