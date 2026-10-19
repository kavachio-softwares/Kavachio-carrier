/** Labels for a file checked against a contract that has since been REPLACED
 *  (renewed, or a new contract uploaded for the same programme and broker).
 *
 *  Each contract keeps its own file history, so a replaced contract's files
 *  stay on every list for audit. Once the newer contract has a file for the
 *  same month, the old file is no longer work: its exceptions are kept but
 *  left out of Exceptions to Review (server: exception_tally.replaced_contract_runs). */

export function ReplacedContractTag({ style }: { style?: React.CSSProperties }) {
  return (
    <span className="badge b-mut" data-testid="replaced-contract"
      style={{ marginLeft: 8, verticalAlign: 1, ...style }}
      title="This file was checked against a contract that has since been replaced. Its history is kept for audit.">
      <span className="d" />Replaced contract
    </span>
  );
}

/** A file from a contract that has since been RENEWED. Its exceptions stay
 *  live work — counted and editable — so this is a label only. */
export function RenewedContractTag({ style }: { style?: React.CSSProperties }) {
  return (
    <span className="badge b-info" data-testid="renewed-contract"
      style={{ marginLeft: 8, verticalAlign: 1, ...style }}
      title="This file was checked against the previous term of a renewed contract. Its exceptions still count and can still be worked.">
      <span className="d" />Renewed contract
    </span>
  );
}

/** Whichever label applies: replaced, renewed, or none. */
export function SupersededTag({ replaced, renewed, style }: {
  replaced?: boolean; renewed?: boolean; style?: React.CSSProperties;
}) {
  if (replaced) return <ReplacedContractTag style={style} />;
  if (renewed) return <RenewedContractTag style={style} />;
  return null;
}

/** The open-exceptions badge for a file that is NOT counted any more: grey,
 *  and saying why, so a number that left the tile never looks lost. */
export function NotCountedBadge({ open, total }: { open: number; total: number }) {
  return (
    <span className="badge b-mut" data-testid="not-counted"
      title={"Kept for audit. The newer contract's file answers this month, so these "
        + "are not counted in Exceptions to Review."}>
      <span className="d" />{open > 0 ? `${open} of ${total} open` : `${total} exceptions`} · not counted
    </span>
  );
}

/** The heading a Versions panel puts above each contract's files, so one
 *  period's history reads as "this contract's Versions 1–15, then the new
 *  contract's Versions 1–4" rather than one running count. */
export function ContractHeading({ name, replaced, renewed }: {
  name?: string | null; replaced?: boolean; renewed?: boolean;
}) {
  return (
    <div data-testid="contract-heading"
      style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
               margin: "16px 0 8px", fontSize: 12.5, fontWeight: 600 }}>
      <span>Contract: {name ?? "not recorded"}</span>
      {replaced || renewed
        ? <SupersededTag replaced={replaced} renewed={renewed} style={{ marginLeft: 0 }} />
        : <span className="badge b-ok"><span className="d" />Current contract</span>}
    </div>
  );
}
