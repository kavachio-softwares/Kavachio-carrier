import { Search } from "lucide-react";

export type SelectFilter = {
  key: string;
  ariaLabel: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
};

export type DateRangeFilter = {
  from: string;
  onFromChange: (v: string) => void;
  to: string;
  onToChange: (v: string) => void;
};

// Generic list toolbar: a search box, any number of dropdown filters, and an
// optional date range — all client-side (no debounce needed, since filtering
// an already-fetched array is synchronous). For a server-side search, wrap
// `search.onChange` in a component using hooks/useDebouncedValue instead;
// the bar itself stays the same either way.
//
// Used across every filterable list page in the app (Tenants, Users,
// Parties, Programs, Uploads, ...) so they share one look and one
// "Clear filters" behavior instead of each page inventing its own.
export function ListFilterBar({
  search, selects, dateRange, onClear, active,
}: {
  search?: { value: string; onChange: (v: string) => void; placeholder?: string };
  selects?: SelectFilter[];
  dateRange?: DateRangeFilter;
  onClear: () => void;
  active: boolean;
}) {
  return (
    <div className="card-h" style={{ gap: 14, flexWrap: "wrap" }}>
      {search && (
        <div className="search">
          <Search className="ic" />
          <input placeholder={search.placeholder ?? "Search…"} value={search.value}
            onChange={e => search.onChange(e.target.value)} />
        </div>
      )}
      {selects?.map(s => (
        <select key={s.key} className="fbar-select" aria-label={s.ariaLabel}
          value={s.value} onChange={e => s.onChange(e.target.value)}>
          {s.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      ))}
      {dateRange && (
        <div className="fbar-daterange">
          <input type="date" className="fbar-date" aria-label="From date"
            value={dateRange.from} onChange={e => dateRange.onFromChange(e.target.value)} />
          <span className="sub">–</span>
          <input type="date" className="fbar-date" aria-label="To date"
            value={dateRange.to} onChange={e => dateRange.onToChange(e.target.value)} />
        </div>
      )}
      {active && (
        <div className="right">
          <span className="linkish" onClick={onClear}>Clear filters</span>
        </div>
      )}
    </div>
  );
}
