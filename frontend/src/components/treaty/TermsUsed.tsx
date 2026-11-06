/**
 * "Terms used" — what a reinsurance statement was worked out with (27.3):
 * every dated term in force on its period's first day, the date each applies
 * from and where it was agreed, and the reinsurers' shares. Each line's own sum
 * is behind the ⓘ on that line, so the two together trace every figure.
 *
 * Shown on the carrier's statement page and, with its own share only, on the
 * reinsurer's private link.
 */
import { Link } from "react-router-dom";
import { ListChecks } from "lucide-react";
import type { BasisChange, CededBasis } from "../../api/ceded";
import { InfoTip } from "../InfoTip";
import { fmtDate } from "../../utils/date";

export function TermsUsed({ basis, contractId, bare = false }: {
  basis: CededBasis | null | undefined;
  /** Carrier side: links to the treaty's Calculation basis. */
  contractId?: number;
  /** Inside something that already has a card (the reinsurer's row). */
  bare?: boolean;
}) {
  const body = !basis ? (
    <div className="sub" style={{ padding: bare ? 0 : "12px 20px" }}>
      Terms used were not recorded for this statement. It was made before they were kept.
    </div>
  ) : (
    <div className="tbl-wrap">
      <table className="panel-tbl" style={{ textAlign: "left" }}>
        <thead>
          <tr><th>Term</th><th>Value</th><th>Applies from</th><th>Where it was agreed</th></tr>
        </thead>
        <tbody>
          {basis.terms.map(t => (
            <tr key={`${t.term}-${t.layer ?? "t"}`}>
              <td>{t.label}{t.layer_name && <span className="sub"> · {t.layer_name}</span>}</td>
              <td><b>{t.value_text}</b></td>
              <td className="mono">{t.effective_from ? fmtDate(t.effective_from) : "—"}</td>
              <td className="sub">{t.source_text}</td>
            </tr>
          ))}
          {basis.layers.flatMap(l => l.shares.map(sh => (
            <tr key={`share-${l.index}-${sh.reinsurer_party_id}`}>
              <td>
                {sh.reinsurer ?? "Reinsurer"} share
                {basis.layers.length > 1 && <span className="sub"> · {l.name}</span>}
              </td>
              <td><b>{sh.share_pct}%</b>{sh.is_lead && <span className="sub"> · lead</span>}</td>
              <td className="mono">—</td>
              <td className="sub">The treaty&rsquo;s panel</td>
            </tr>
          )))}
        </tbody>
      </table>
    </div>
  );
  if (bare) return body;
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-h">
        <ListChecks size={16} className="ci" />
        <h3>
          Terms used
          <InfoTip text={"Every figure on this statement was worked out with these terms: "
            + "the ones in force on the first day of the period. The ⓘ on each line "
            + "shows its sum."} />
        </h3>
        {basis?.as_of && <span className="sub">in force on {fmtDate(basis.as_of)}</span>}
        {contractId != null && (
          <Link className="linkish right" to={`/contracts/${contractId}?tab=basis`}
                style={{ fontSize: 13 }}>See the Calculation basis</Link>
        )}
      </div>
      {body}
    </div>
  );
}

/** "Cession: 40% → 35% from 1 Jul 2026" — a term that differs now. */
export function changeText(c: BasisChange): string {
  const what = c.layer_name ? `${c.label} (${c.layer_name})` : c.label;
  return `${what}: ${c.was} → ${c.now}`
    + (c.effective_from ? ` from ${fmtDate(c.effective_from)}` : "");
}
