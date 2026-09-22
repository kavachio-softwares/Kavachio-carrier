/**
 * The operator's day.
 *
 * An operator is a seat the BROKER adds to do the day-to-day work — the chain
 * is Kavachio, then carrier, then broker, then operator. Their scope is the
 * broker's: same carriers, same programmes. What differs is the question. A
 * broker admin asks "what is holding me up"; an operator asks "what do I have
 * to run, and what went wrong".
 *
 * The counts are real. When there is nothing to run yet the screen says which
 * step is missing and whose job it is, rather than showing an empty table that
 * looks like a fault.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { getOperatorHome, type OperatorHome as Home, type OperatorRun } from "../api/broker";
import { fmtDateTime } from "../utils/date";

/** The exception screen a run opens on — the same one Process Bordereau uses. */
const reviewPath = (r: OperatorRun) =>
  `/uploads/${r.export_id}/exceptions?download=${r.export_id}&from=broker`;

export default function OperatorHome() {
  const nav = useNavigate();
  const [d, setD] = useState<Home | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getOperatorHome().then(setD).catch(() => setErr("Could not load your dashboard."));
  }, []);

  const head = (
    <div className="page-head">
      <div className="t">
        <h2>Dashboard</h2>
        <p>
          {d ? `${d.broker.name} — what needs doing today, and the spreadsheets you ran most recently.`
             : "What needs doing today."}
        </p>
      </div>
    </div>
  );

  if (err) return (
    <div className="proto"><div className="view full">{head}
      <div className="note warn" style={{ maxWidth: 560 }}>{err}</div>
    </div></div>
  );
  if (!d) return (
    <div className="proto"><div className="view full">{head}
      <div className="muted">Loading…</div>
    </div></div>
  );

  const c = d.counts;
  const carrierNames = d.carriers.map(x => x.name).join(", ") || "—";
  // The newest run that still has something to look at — where the
  // exceptions tile takes you.
  const firstToReview = d.recent_runs.find(r => r.exception_count > 0);

  return (
    <div className="proto">
      <div className="view full">
        {head}

        <div className="tiles" style={{ marginBottom: 18 }}>
          <div className={`tile${c.exceptions > 0 ? " alert" : ""}`}>
            <div className="k">Exceptions to review</div>
            <div className="v">{c.exceptions}</div>
            <div className="foot">
              {c.exceptions > 0
                ? <>
                    in {c.exception_runs} {c.exception_runs === 1 ? "run" : "runs"}
                    {firstToReview && <>{" · "}
                      <Link className="linkish" to={reviewPath(firstToReview)}>Review →</Link></>}
                  </>
                : "rows that failed a check"}
            </div>
          </div>
          <div className="tile">
            <div className="k">Runs</div>
            <div className="v">{c.runs}</div>
            <div className="foot">files run for your broker, by your team or the carrier</div>
          </div>
          <div className="tile">
            <div className="k">Setups you can use</div>
            <div className="v">{c.setups}</div>
            <div className="foot">built by your broker admin</div>
          </div>
          <div className="tile">
            <div className="k">Programmes</div>
            <div className="v">{c.programmes}</div>
            <div className="foot">{carrierNames}</div>
          </div>
        </div>

        {/* Why there is nothing to do, and whose job the next step is. An
            operator cannot unblock either of these themselves, so saying
            "no runs yet" alone would leave them stuck. */}
        {d.blocked_on === "no-programme" && (
          <div className="card pad" style={{ maxWidth: 640 }}>
            <h3 style={{ margin: "0 0 8px", fontSize: 14 }}>Nothing to run yet</h3>
            <p className="muted" style={{ margin: 0, fontSize: 13, lineHeight: 1.6 }}>
              Your broker has not been put on a carrier's programme yet. Until a
              carrier does that, there is no work to do here — and it is not
              something you or your broker admin can do from this side.
            </p>
          </div>
        )}

        {d.blocked_on === "no-setup" && (
          <div className="card pad" style={{ maxWidth: 640 }}>
            <h3 style={{ margin: "0 0 8px", fontSize: 14 }}>Nothing to run yet</h3>
            <p className="muted" style={{ margin: 0, fontSize: 13, lineHeight: 1.6 }}>
              You are on {c.programmes === 1 ? "a programme" : `${c.programmes} programmes`} for{" "}
              {carrierNames}, but no bordereau setup has been built yet. A setup
              is what tells Kavachio how to read your spreadsheet, and the
              CARRIER builds it — it is what defines a valid file, so it is
              theirs to decide. Nobody on your side can unblock this; ask your
              carrier contact.
            </p>
          </div>
        )}

        {/* Shown whenever there are runs — a run the carrier made for this
            broker is there to be reviewed even before a setup of the
            broker's own is in place. */}
        {(d.blocked_on === null || d.recent_runs.length > 0) && (
          <div className="card">
            <div className="card-h">
              <h3>Recent runs</h3>
              <span className="muted" style={{ fontSize: 12 }}>
                files run for your broker, by your team or the carrier
              </span>
            </div>
            {d.recent_runs.length === 0 ? (
              <div className="empty">
                No runs yet — process a bordereau and it will appear here.
                <div style={{ marginTop: 12 }}>
                  <Link className="btn pri" to="/broker/bordereau">Process a bordereau</Link>
                </div>
              </div>
            ) : (
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>File</th><th>Programme</th><th>Contract</th>
                      <th>Sent by</th><th>Rows</th><th>Result</th><th>When</th><th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.recent_runs.map(r => (
                      <tr key={r.export_id}>
                        <td><b>{r.filename}</b></td>
                        <td>{r.programme ?? "—"}</td>
                        <td className="muted">{r.contract ?? "—"}</td>
                        <td>{r.sent_by === "carrier" ? "The carrier" : "Your team"}</td>
                        <td>{r.rows ?? "—"}</td>
                        <td>
                          {r.exception_count > 0
                            ? <span className="badge b-warn"><span className="d" />
                                {r.exception_count} {r.exception_count === 1 ? "exception" : "exceptions"}</span>
                            : r.status === "not_validated"
                              ? <span className="badge"><span className="d" />Not checked</span>
                              : <span className="badge b-ok"><span className="d" />Clean</span>}
                        </td>
                        <td className="muted">{fmtDateTime(r.created_at)}</td>
                        <td>
                          <button className="btn sm" onClick={() => nav(reviewPath(r))}>
                            {r.exception_count > 0 ? "Review →" : "Open →"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
