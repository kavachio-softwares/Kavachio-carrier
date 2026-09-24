/**
 * Which broker company this seat belongs to — the broker's counterpart to the
 * carrier admin's workspace card.
 *
 * A carrier user's sidebar names their own organisation under the Kavachio
 * mark. A broker user had nothing there: the only organisation named anywhere
 * in their sidebar was the CARRIER they are working on (see
 * BrokerCarrierSwitch), so a broker operator could not tell whose books they
 * were producing on, and neither could anyone looking over their shoulder.
 * My Account did not answer it either — its Organization field reads the
 * tenant code, which a broker seat has none of, so it showed a dash.
 *
 * Read-only on purpose. The broker never picks their own company — it is read
 * off their user row — so this is identity, not a control, and it carries no
 * chevron and nothing to click.
 *
 * It sits ABOVE the carrier switch, so the sidebar reads top to bottom as
 * "who I am, then who I am working for".
 */
import { useEffect, useState } from "react";
import { getBrokerMe } from "../api/brokerBordereau";
import { initials } from "../branding";

export function BrokerOrgCard() {
  const [name, setName] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getBrokerMe()
      .then(me => { if (!cancelled) setName(me.name || null); })
      // The sidebar simply stays as it was — a failed lookup must not take the
      // nav down with it.
      .catch(() => { /* no card */ });
    return () => { cancelled = true; };
  }, []);

  // "—" is what /broker/me returns when the party row is missing. Showing it
  // would be a card that names nothing.
  if (!name || name === "—") return null;

  return (
    <div className="wscard wscard-static" title={name}>
      <span className="wscard-ava">
        <span className="wscard-logo wscard-fallback">{initials(name)}</span>
        <span className="wscard-dot" aria-hidden="true" />
      </span>
      <span className="wscard-meta">
        <span className="wscard-name">{name}</span>
        <span className="wscard-sub">Broker</span>
      </span>
    </div>
  );
}
