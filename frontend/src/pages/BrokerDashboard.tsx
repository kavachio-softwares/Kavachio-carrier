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
import { getBrokerDashboard, type BrokerDashboard as Dash } from "../api/broker";
import { inAppSigningUrl } from "../api/esign";

export default function BrokerDashboard() {
  const nav = useNavigate();
  const [d, setD] = useState<Dash | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getBrokerDashboard().then(setD)
      .catch(() => setErr("Could not load your dashboard."));
  }, []);

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
        {c.programmes === 0 ? (
          <div className="card pad" style={{ maxWidth: 620 }}>
            <h3 style={{ margin: "0 0 8px", fontSize: 14 }}>No programmes yet</h3>
            <p className="muted" style={{ margin: 0, fontSize: 13, lineHeight: 1.6 }}>
              A carrier has to put you on one of their programmes before you can
              send anything. Until they do, there is nothing for you to set up —
              this is not something you can do from your side.
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
