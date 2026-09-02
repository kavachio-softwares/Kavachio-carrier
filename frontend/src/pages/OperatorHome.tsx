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
import { getOperatorHome, type OperatorHome as Home } from "../api/broker";

export default function OperatorHome() {
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

  return (
    <div className="proto">
      <div className="view full">
        {head}

        <div className="tiles" style={{ marginBottom: 18 }}>
          <div className={`tile${c.exceptions > 0 ? " alert" : ""}`}>
            <div className="k">Exceptions to review</div>
            <div className="v">{c.exceptions}</div>
            <div className="foot">rows that failed a check</div>
          </div>
          <div className="tile">
            <div className="k">Runs</div>
            <div className="v">{c.runs}</div>
            <div className="foot">files you have put through</div>
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
              {carrierNames}, but no BDX setup has been built yet. A setup is what
              tells Kavachio how to read your spreadsheet — your broker admin
              builds it once per contract, and then you can process files against it.
            </p>
          </div>
        )}

        {d.blocked_on === null && (
          <div className="card">
            <div className="card-h">
              <h3>Recent runs</h3>
              <span className="muted" style={{ fontSize: 12 }}>
                the spreadsheets you ran most recently
              </span>
            </div>
            <div className="empty">
              No runs yet — process a bordereau and it will appear here.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
