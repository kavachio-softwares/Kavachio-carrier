/**
 * A treaty, drawn — the same picture on every screen that shows one.
 *
 * Non-proportional (XOL, stop loss, and surplus by its lines): a TOWER. The
 * carrier's retention at the bottom, each layer stacked above it, and each
 * band filled to how much of it is placed. Proportional (quota share,
 * facultative): a SPLIT, ceded against retained.
 *
 * Bands are not drawn to scale. A 1m retention under a 100m layer would be a
 * hairline, and the point of the picture is the ORDER of the layers and how
 * placed each one is — the amounts are printed on each band instead.
 *
 * `mine` marks the layers a reader holds a share of — the reinsurer's page
 * passes theirs, so they see where their lines sit in the whole programme.
 */
import type { TreatySummary } from "../../api/treaty";

function amount(n: number, unit: "money" | "pct", cur: string | null): string {
  if (unit === "pct") return `${n % 1 === 0 ? n : n.toFixed(2)}%`;
  const short = n >= 1_000_000
    ? `${+(n / 1_000_000).toFixed(2)}m`
    : n >= 1_000 ? `${+(n / 1_000).toFixed(1)}k` : `${n}`;
  return cur ? `${cur} ${short}` : short;
}

/** A percentage as people write it: 6, 2.5, 0.75 — never 6.000000001. */
const pc = (n: number) => `${+n.toFixed(2)}%`;

export function TreatyTower({ summary, mine, compact, carrier, yourShare }: {
  summary: TreatySummary;
  mine?: Set<number>;
  compact?: boolean;
  /** The carrier's name, when the reader is NOT the carrier — a reinsurer on
   *  its private link. Everything is then said in the third person. */
  carrier?: string;
  /** The reader's own share of the ceded part, as a reinsurer signed it. */
  yourShare?: number | null;
}) {
  const retained = carrier ? `${carrier}'s retention` : "Your retention";
  if (summary.split) {
    const { ceded_pct: c, retained_pct: r } = summary.split;
    return (
      <div>
        <div className="tw-split" role="img"
             aria-label={`${c}% to reinsurers, ${r}% kept`}>
          {c > 0 && <div className="ced" style={{ flex: c }}>{pc(c)} to reinsurers</div>}
          {r > 0 && <div className="ret" style={{ flex: r }}>
            {pc(r)} kept{carrier ? "" : " by you"}</div>}
        </div>
        {!compact && (() => {
          // Three different percentages live in a quota share — the share of
          // each risk that is ceded, how much of THAT the reinsurers have
          // signed for, and one reinsurer's own line of it. Each is said once,
          // in a sentence, and turned into "of every risk" so they can be
          // compared without arithmetic.
          const placed = summary.placed_pct;
          const placedOfRisk = (c * placed) / 100;
          const who = carrier ?? "You";
          return (
            <ul className="tw-explain">
              <li>
                <b>{who}</b> {carrier ? "passes" : "pass"} <b>{pc(c)}</b> of every
                risk to reinsurers and {carrier ? "keeps" : "keep"} the
                other <b>{pc(r)}</b>.
              </li>
              <li>
                {placed >= 99.995
                  ? <>Reinsurers have signed for <b>all</b> of that {pc(c)}.</>
                  : <>Reinsurers have signed for <b>{pc(placed)}</b> of that {pc(c)} so
                     far — {pc(placedOfRisk)} of every risk. The remaining{" "}
                     {pc(c - placedOfRisk)} is not placed yet.</>}
              </li>
              {yourShare != null && (
                <li>
                  <b>Your share</b> is {pc(yourShare)} of the {pc(c)} passed on —
                  that is <b>{pc((c * yourShare) / 100)} of every risk</b>.
                </li>
              )}
            </ul>
          );
        })()}
      </div>
    );
  }
  if (!summary.tower || !summary.tower.bands.length) {
    return <div className="sub">Fill in the layers to see the tower.</div>;
  }
  const { unit, currency, bands } = summary.tower;
  return (
    <div>
      <div className="tw" role="list" aria-label="Treaty layers, bottom to top">
        {bands.map((b, i) => {
          const placed = b.placed_pct ?? 0;
          const isLayer = b.kind === "layer";
          const full = isLayer && placed >= 99.995;
          const isMine = isLayer && b.layer !== undefined && mine?.has(b.layer);
          return (
            <div key={i} role="listitem"
                 className={`tw-band ${isLayer ? "" : "ret"} ${full ? "full" : ""} `
                   + `${isMine ? "mine" : ""}`}
                 style={compact ? { minHeight: 30, padding: "5px 10px" } : undefined}>
              {isLayer && (
                <span className="fill" style={{ width: `${Math.min(placed, 100)}%` }} />
              )}
              <span className="lbl">
                {isLayer ? b.label : retained}
                {isMine && <span className="badge b-ok" style={{ marginLeft: 8 }}>
                  <span className="d" />You are on this layer</span>}
              </span>
              <span className="rng">
                {amount(b.from, unit, currency)} → {amount(b.to, unit, currency)}
              </span>
              {isLayer && (
                <span className={`pl ${full ? "" : "short"}`}>
                  {placed % 1 === 0 ? placed : placed.toFixed(2)}% placed
                </span>
              )}
            </div>
          );
        })}
      </div>
      {!compact && (
        <div className="tw-legend">
          <span><i style={{ background: "var(--p-surface-3)" }} />{retained}</span>
          <span><i style={{ background: "var(--p-primary-soft)" }} />Placed share of a layer</span>
          <span>Bands are in order, not to scale.</span>
        </div>
      )}
    </div>
  );
}

/** A thin bar for "how placed is it", used in tables. */
export function PlaceBar({ pct }: { pct: number }) {
  const cls = pct > 100.005 ? "over" : pct < 99.995 ? "short" : "";
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <span className={`placebar ${cls}`} aria-hidden>
        <i style={{ width: `${Math.min(pct, 100)}%` }} />
      </span>
      <span className="mono" style={{ fontSize: 12 }}>
        {pct % 1 === 0 ? pct : pct.toFixed(2)}%
      </span>
    </span>
  );
}
