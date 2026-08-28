// Pairs with hooks/useServerList (true server-side pagination) — same
// "X–Y of Z" + Prev/Next controls used across every paginated list in the
// app, so they all look and behave alike.
export function Pagination({
  page, pageCount, pageSize, totalItems, onPageChange, noun = "items",
}: {
  page: number; pageCount: number; pageSize: number; totalItems: number;
  onPageChange: (page: number) => void; noun?: string;
}) {
  if (totalItems === 0) return null;
  return (
    <div className="card-h" style={{ justifyContent: "space-between", borderBottom: "none", borderTop: "1px solid var(--p-border)" }}>
      <span className="sub">
        {(page - 1) * pageSize + 1}–{Math.min(page * pageSize, totalItems)} of {totalItems} {noun}
      </span>
      <div style={{ display: "flex", gap: 8 }}>
        <button className="btn sm" disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}>← Prev</button>
        <span className="sub" style={{ alignSelf: "center" }}>
          Page {page} of {pageCount}
        </span>
        <button className="btn sm" disabled={page >= pageCount}
          onClick={() => onPageChange(page + 1)}>Next →</button>
      </div>
    </div>
  );
}
