/**
 * Which carrier the broker is working on — the sidebar's counterpart to the
 * carrier admin's workspace card.
 *
 * A carrier admin's sidebar names the one organisation they are in. A broker
 * works with several, so the same slot has to name the one they are working on
 * NOW and let them change it. Everything below it in the nav — the dashboard's
 * counts, their contracts, the bordereau they are about to process — is scoped
 * by this, which is why it sits above the nav rather than inside a page: a
 * scope control living on one screen looks like that screen's filter.
 *
 * It renders NOTHING for a broker on a single carrier. A control whose only
 * option is the one already in force is furniture, and it would suggest a
 * decision exists where none does.
 *
 * THERE IS NO "ALL CARRIERS". A broker works on one carrier at a time, and a
 * merged view is the state in which a file gets processed against the wrong
 * carrier's contract because two lists looked alike. So one is always
 * selected — the first, until they choose otherwise — and every screen below
 * is scoped to it. The switcher changes which; it cannot turn the scope off.
 */
import { useEffect, useRef, useState } from "react";
import { Check, ChevronsUpDown } from "lucide-react";
import { getBrokerCarriers, type BrokerCarrier } from "../api/broker";
import {
  getBrokerCarrierId, setBrokerCarrierId, useBrokerCarrierId,
} from "../brokerCarrier";
import { initials } from "../branding";

export function BrokerCarrierSwitch() {
  const [carriers, setCarriers] = useState<BrokerCarrier[]>([]);
  const [open, setOpen] = useState(false);
  const selected = useBrokerCarrierId();
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getBrokerCarriers()
      .then(list => {
        setCarriers(list);
        // A selection that is no longer valid — the carrier took this broker
        // off everything — silently becomes "all carriers". Leaving it would
        // show empty screens with no way to tell why.
        // One is ALWAYS selected. A stored id that is no longer valid — the
        // carrier took this broker off everything — falls back to the first
        // rather than to nothing, because nothing now means an unscoped screen
        // and there is no such thing.
        const cur = getBrokerCarrierId();
        if (list.length && (cur == null || !list.some(c => c.id === cur))) {
          setBrokerCarrierId(list[0].id);
        }
      })
      .catch(() => setCarriers([]));
  }, []);

  // Click-away and Escape, so the menu cannot be left open over the nav it
  // covers.
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  if (carriers.length < 2) return null;

  const current = carriers.find(c => c.id === selected) ?? carriers[0];
  const label = current?.name ?? "";

  function pick(id: number) {
    setBrokerCarrierId(id);
    setOpen(false);
  }

  return (
    <div className="ws-scope" ref={box}>
      <button
        type="button"
        className="wscard"
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Switch carrier"
        onClick={() => setOpen(o => !o)}
      >
        <span className="wscard-ava">
          <span className="wscard-logo wscard-fallback">
            {current ? initials(current.name) : ""}
          </span>
          <span className="wscard-dot" aria-hidden="true" />
        </span>
        <span className="wscard-meta">
          <span className="wscard-name">{label}</span>
          <span className="wscard-sub">
            {current
              ? (current.programme_count === 1
                  ? "1 programme" : `${current.programme_count} programmes`)
              : ""}
          </span>
        </span>
        <ChevronsUpDown className="wscard-go" size={15} strokeWidth={2} />
      </button>

      {open && (
        <div className="ws-menu" role="listbox">
          {carriers.map(c => (
            <button
              key={c.id} type="button" role="option"
              aria-selected={c.id === selected}
              className={c.id === selected ? "on" : ""}
              onClick={() => pick(c.id)}
            >
              <span className="nm">{c.name}</span>
              <span className="ct">
                {c.programme_count === 1
                  ? "1 programme" : `${c.programme_count} programmes`}
              </span>
              {c.id === selected && <Check size={13} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
