/**
 * The ITD half of a run's result (RunResult): was the file read as
 * inception-to-date, what did the comparison with earlier files find — in the
 * client's classes — and how many rows went on. One tile per class, so a
 * "0 rows in output BDX" is read next to the 320 rows already sent before it.
 */
import type { CSSProperties, ReactNode } from "react";
import {
  ITD_CLASS_LABEL, itdClassCounts, type ItdClass, type ItdResult, type ItdSummary,
} from "../api/itd";
import { periodLabel } from "../utils/date";

export type ItdRunInfo = {
  load_type: "itd" | "regular";
  as_at: string | null;
  reason: string | null;
  summary: ItdSummary;
  check_only: boolean;
  conflicts?: number;
  apply_error?: string | null;
};

const n = (v: number) => v.toLocaleString();
const plural = (v: number, one: string, many = `${one}s`) => (v === 1 ? one : many);

/** The colour along the top of a class's tile. */
const TONE: Record<ItdClass, string> = {
  new: "var(--p-ok)",
  previously_processed: "var(--p-faint)",
  changed: "var(--p-info)",
  cancelled_or_reversed: "var(--p-info)",
  held_back: "var(--p-warn)",
};

/** ["2 amount — difference sent", "1 details only — listed"] — the parts
 *  that hold anything, one line each. */
function parts(xs: [number, string][]): string[] {
  return xs.filter(([v]) => v > 0).map(([v, w]) => `${n(v)} ${w}`);
}

/** What each class holds and what happened to it, under its number. */
function classNote(c: ItdClass, s: ItdSummary): string[] {
  const onTwoTabs = s.duplicate_other_sheet ?? 0;
  switch (c) {
    case "new": return s.new > 0 ? ["in output BDX"] : [];
    case "previously_processed": return s.unchanged > 0 ? ["already sent — not sent again"] : [];
    case "changed": return parts([[s.changed, "amount — difference sent"],
                                  [s.details_changed, "details only — listed"]]);
    case "cancelled_or_reversed": return parts([
      [s.reversal ?? 0, `${plural(s.reversal ?? 0, "reversal")} — sent`],
      [s.dropped, "missing from this file — to decide"]]);
    case "held_back": return parts([[onTwoTabs, "on two tabs, counted once"],
                                    [s.duplicate - onTwoTabs, "duplicated in the file"],
                                    [s.missing_key, "can't identify"]]);
  }
}

const tileStyle = (c: ItdClass): CSSProperties => ({
  background: "var(--p-surface)", border: "1px solid var(--p-border)",
  borderTop: `3px solid ${TONE[c]}`, borderRadius: "var(--p-r-sm)", padding: "10px 12px",
  minWidth: 0,
});

function Badge({ itd }: { itd: boolean }) {
  return itd
    ? <span className="badge b-info"><span className="d" />ITD</span>
    : <span className="badge b-mut"><span className="d" />Regular</span>;
}

export function ItdRunCard({ itd }: { itd: ItdRunInfo }) {
  const s = itd.summary;
  const isItd = itd.load_type === "itd";
  const asAt = itd.as_at ? periodLabel(itd.as_at) : null;
  const counts = itdClassCounts(s as unknown as Partial<Record<ItdResult, number>>);
  // The client's four classes always show, so a 0 reads as an answer;
  // Skipped only when something was skipped.
  const shown = (Object.keys(ITD_CLASS_LABEL) as ItdClass[])
    .filter(c => c !== "held_back" || counts.held_back > 0);
  const sent = s.rows_to_load;
  const late = s.late_rows ?? 0;
  // The rows that go into the output BDX — what Download BDX holds and what
  // the contract rules check. A self-check records and sends nothing — it
  // says what a real run would do.
  const intoBdx = itd.check_only ? "would be in output BDX" : "in output BDX";

  const headline: ReactNode = isItd
    ? <>Inception-to-date file{asAt && <> · as at <b>{asAt}</b></>}</>
    : <>Monthly file{asAt && <> · <b>{asAt}</b></>}</>;

  return (
    <div className="card" style={{ marginBottom: 18, padding: "14px 16px" }}>
      {/* What the file is, and the one number that matters: rows in the output BDX. */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <Badge itd={isItd} />
        <span style={{ fontSize: 13.5, color: "var(--p-ink)" }}>{headline}</span>
        {itd.check_only && <span className="badge b-mut">Self-check — nothing recorded</span>}
        <span style={{ marginLeft: "auto", fontSize: 13, color: "var(--p-muted)",
                       whiteSpace: "nowrap" }}>
          {n(s.rows_in_file)} {plural(s.rows_in_file, "row")} in file
          <span style={{ margin: "0 6px", color: "var(--p-faint)" }}>→</span>
          <b style={{ color: sent > 0 ? "var(--p-ok-ink)" : "var(--p-ink)" }}>
            {n(sent)} {plural(sent, "row")} {intoBdx}</b>
        </span>
      </div>

      {isItd ? (
        <>
          <div style={{ display: "grid", gap: 10, marginTop: 12,
                        gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" }}>
            {shown.map(c => {
              const notes = classNote(c, s);
              return (
                <div key={c} style={tileStyle(c)}>
                  <div style={{ fontSize: 11.5, fontWeight: 600, color: "var(--p-muted)" }}>
                    {ITD_CLASS_LABEL[c]}</div>
                  <div style={{ fontFamily: "var(--p-disp)", fontSize: 22, fontWeight: 600,
                                lineHeight: 1.1, marginTop: 4,
                                color: counts[c] > 0 ? "var(--p-ink)" : "var(--p-faint)" }}>
                    {n(counts[c])}</div>
                  {notes.map(t => (
                    <div key={t} style={{ fontSize: 11.5, color: "var(--p-faint)", marginTop: 4,
                                          lineHeight: 1.35 }}>{t}</div>))}
                </div>
              );
            })}
          </div>
          {sent === 0 && (
            <div className="note ok" style={{ marginTop: 10, padding: "8px 12px" }}>
              Nothing new — every row in this file was already sent in an earlier file.
            </div>)}
        </>
      ) : (
        <div style={{ fontSize: 13, color: "var(--p-muted)", marginTop: 8 }}>
          All {n(s.rows_in_file)} {plural(s.rows_in_file, "row")} {intoBdx}
          {late > 0 && <> · <b style={{ color: "var(--p-ink)" }}>{n(late)} late{" "}
            {plural(late, "transaction")}</b> reported this month, loaded as usual</>}
          {" "}— recorded so later ITD files don't repeat them.
        </div>
      )}

      {itd.reason && (
        <div style={{ fontSize: 12, color: "var(--p-muted)", marginTop: 10, paddingTop: 10,
                      borderTop: "1px solid var(--p-border)", lineHeight: 1.5 }}>
          <b style={{ color: "var(--p-ink)" }}>Why {isItd ? "ITD" : "monthly"}:</b> {itd.reason}
        </div>)}

      {(s.identical_rows ?? 0) > 0 && (
        <div className="note warn" style={{ marginTop: 10 }}>
          <strong>{n(s.identical_rows!)} identical {plural(s.identical_rows!, "row")} to
            confirm</strong> — no transaction number, and the same policy, type, date,
          period and amounts as another row of this file. All were sent: please confirm
          they are real transactions and not one row sent twice.
        </div>)}
      {(itd.conflicts ?? 0) > 0 && (
        <div className="note warn" style={{ marginTop: 10 }}>
          <strong>{n(itd.conflicts!)} {plural(itd.conflicts!, "row")} could not be recorded in
            the running total</strong> — another file changed the same transactions at the
          same time.
        </div>)}
      {/* The server's own words stay in the tooltip — they are for support,
          not for the person reading the result. */}
      {itd.apply_error && (
        <div className="note warn" style={{ marginTop: 10 }} title={itd.apply_error}>
          <strong>The running total could not be updated.</strong> The output above is
          complete, but the next ITD file may repeat rows from this one.
        </div>)}
    </div>
  );
}
