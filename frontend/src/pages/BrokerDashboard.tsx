/**
 * The broker's landing screen.
 *
 * A broker holds no book of its own — it produces into carriers' programmes.
 * So this screen answers two questions and no others: what have I been given,
 * and what is waiting on me — terms to read, or a signature to give. There is
 * no queue pointing the other way any more: the carrier's approval gate is
 * gone, so nothing a broker adds sits waiting for an answer.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  getBrokerDashboard, getBrokerInvitations, acceptBrokerInvitation,
  declineBrokerInvitation,
  type BrokerDashboard as Dash, type BrokerInvitation,
} from "../api/broker";
import { useBrokerCarrierId } from "../brokerCarrier";
import { fmtDate } from "../utils/date";
import { inAppSigningUrl } from "../api/esign";

export default function BrokerDashboard() {
  const nav = useNavigate();
  const [d, setD] = useState<Dash | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // The carrier this broker is working on, chosen in the sidebar. Every count
  // below is scoped to it — "two waiting on you" has to mean two on THIS
  // carrier, or the number is answering a question nobody asked.
  const carrierId = useBrokerCarrierId();

  // Carriers asking to work with this broker. Above everything else on the
  // page, because until one is answered nothing else about that carrier
  // exists — no programmes, no contracts, no files.
  const [invites, setInvites] = useState<BrokerInvitation[] | null>(null);
  const [answering, setAnswering] = useState<number | null>(null);
  const [note, setNote] = useState("");

  const loadInvites = () =>
    getBrokerInvitations().then(setInvites).catch(() => setInvites([]));
  useEffect(() => { loadInvites(); }, []);

  async function answer(id: number, accept: boolean) {
    setAnswering(id); setNote("");
    try {
      const r = accept ? await acceptBrokerInvitation(id)
                       : await declineBrokerInvitation(id);
      setNote(r?.message ?? (accept ? "Accepted." : "Declined."));
      await loadInvites();
      // Accepting adds a carrier, so the counts and the switcher are stale.
      getBrokerDashboard(carrierId).then(setD).catch(() => {});
    } catch {
      setNote("That did not go through. Try again.");
    } finally { setAnswering(null); }
  }

  useEffect(() => {
    setD(null);
    getBrokerDashboard(carrierId).then(setD)
      .catch(() => setErr("Could not load your dashboard."));
  }, [carrierId]);

  if (err) return (
    <div className="proto"><div className="view full">
      <div className="page-head"><div className="t"><h2>Dashboard</h2></div></div>
      <div className="note warn" style={{ maxWidth: 560 }}>{err}</div>
    </div></div>
  );
  if (!d) return (
    <div className="proto"><div className="view full">
      <div className="page-head"><div className="t"><h2>Dashboard</h2></div></div>
      <div className="muted">Loading…</div>
    </div></div>
  );

  const c = d.counts;
  const carrierNames = d.carriers.map(x => x.name).join(", ");

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Dashboard</h2>
            <p>
              {d.broker.name} — the programmes you send business to, and
              anything that is holding you up.
            </p>
          </div>
        </div>

        {/* Nothing assigned yet is a real state, not an error. Say who fixes it. */}
        {/* Above the no-programmes branch on purpose. A broker who has only
            been invited has no programmes yet — that is precisely the state
            this is for, and inside that branch they would never see it. */}
        {note && (
          <div className="note ok" style={{ marginBottom: 16 }}>{note}</div>
        )}

        {!!invites?.length && (
          <div className="card" style={{ marginBottom: 18 }}>
            <div className="card-h">
              <h3>
                {invites.length === 1
                  ? "A carrier wants to work with you"
                  : `${invites.length} carriers want to work with you`}
              </h3>
              <span className="sub">nothing happens until you answer</span>
            </div>
            <div style={{ padding: "14px 20px" }}>
              {invites.map(iv => (
                <div className="kv" key={iv.id}>
                  <span className="k">
                    <b style={{ color: "var(--p-ink)" }}>{iv.carrier}</b>
                    <div className="sub">
                      {iv.programme
                        ? `Invited you on to ${iv.programme}`
                        : "Invited you to work with them"}
                      {iv.invited_at && <> · {fmtDate(iv.invited_at)}</>}
                    </div>
                  </span>
                  <span style={{ display: "flex", gap: 8 }}>
                    <button className="btn sm" type="button"
                            disabled={answering === iv.id}
                            onClick={() => answer(iv.id, false)}>
                      Decline
                    </button>
                    <button className="btn sm pri" type="button"
                            disabled={answering === iv.id}
                            onClick={() => answer(iv.id, true)}>
                      {answering === iv.id ? "…" : "Accept"}
                    </button>
                  </span>
                </div>
              ))}
              <div className="hint" style={{ marginTop: 10 }}>
                Accepting lets them put you on their programmes. It shows
                them nothing about the other carriers you work with.
              </div>
            </div>
          </div>
        )}

        {c.programmes === 0 ? (
          <div className="card pad" style={{ maxWidth: 620 }}>
            <h3 style={{ margin: "0 0 8px", fontSize: 14 }}>No programmes yet</h3>
            <p className="muted" style={{ margin: 0, fontSize: 13, lineHeight: 1.6 }}>
              {invites?.length
                ? "Accept the invitation above and that carrier can start "
                  + "putting you on their programmes."
                : "A carrier has to put you on one of their programmes before "
                  + "you can send anything. Until they do, there is nothing "
                  + "for you to set up — this is not something you can do from "
                  + "your side."}
            </p>
          </div>
        ) : (
          <>
            <div className="tiles" style={{ marginBottom: 18 }}>
              {/* Your queue first. It is the one nobody else can move, and it
                  was the one this dashboard never showed. */}
              <div className={`tile${c.waiting_on_me > 0 ? " alert" : ""}`}>
                <div className="k">Waiting on you</div>
                <div className="v">{c.waiting_on_me}</div>
                <div className="foot">terms to read, or a signature to give</div>
              </div>
              <div className="tile">
                <div className="k">Live contracts</div>
                <div className="v">{c.live_contracts}</div>
                <div className="foot">in force — ready to set up</div>
              </div>
              <div className="tile">
                <div className="k">Programmes you're on</div>
                <div className="v">{c.programmes}</div>
                <div className="foot">given to you by the carrier</div>
              </div>
              <div className="tile">
                <div className="k">{c.carriers === 1 ? "Carrier" : "Carriers"}</div>
                <div className="v">{c.carriers}</div>
                <div className="foot">{carrierNames || "—"}</div>
              </div>
            </div>

            {/* The broker's only queue. A negotiation that does not announce
                itself is one nobody answers. */}
            {d.waiting_on_me.length === 0 ? (
              <div className="card pad">
                <p className="muted" style={{ margin: 0, fontSize: 13 }}>
                  Nothing is waiting on you. Your contracts are on{" "}
                  <span className="linkish" onClick={() => nav("/broker/contracts")}>
                    My Contracts
                  </span>.
                </p>
              </div>
            ) : (
              <div className="card" style={{ marginBottom: 18 }}>
                <div className="card-h">
                  <h3>Waiting on you</h3>
                  <span className="muted" style={{ fontSize: 12 }}>
                    nothing moves on these until you answer
                  </span>
                </div>
                <div className="tbl-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Contract</th><th>Programme</th><th>Carrier</th>
                        <th>What to do</th><th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.waiting_on_me.map(w => (
                        <tr key={w.id}>
                          <td>
                            <Link to={`/contracts/${w.id}`}><b>{w.name}</b></Link>
                          </td>
                          <td>{w.programme}</td>
                          <td>{w.carrier}</td>
                          <td className="muted">{w.what}</td>
                          <td>
                            {/* A contract waiting on the broker's SIGNATURE
                                gets the signing page itself, not the record.
                                By the time it reaches this queue the carrier
                                has already signed — that is what put it here —
                                so the next thing to happen is the broker
                                signing, and one click short of it is one click
                                too many. Everything else still opens the
                                contract, because reading it IS the job. */}
                            {(w.lifecycle === "agreed" || w.lifecycle === "signed") ? (
                              <a className="btn sm pri"
                                 href={inAppSigningUrl(w.id)}
                                 target="_blank" rel="noreferrer">
                                Sign it →
                              </a>
                            ) : (
                              <Link className="btn sm pri" to={`/contracts/${w.id}`}>
                                Open →
                              </Link>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            <div className="note" style={{ marginTop: 16 }}>
              <b>You do not create carriers or programmes.</b> The carrier puts you
              on a programme, and everything you can reach follows from that. Your
              contracts are on{" "}
              <span className="linkish" onClick={() => nav("/broker/contracts")}>
                My Contracts
              </span>.
            </div>
          </>
        )}
      </div>
    </div>
  );
}
