import { useCallback, useEffect, useRef, useState } from "react";

// TRUE server-side pagination: the backend applies filters + LIMIT/OFFSET and
// returns one page plus the matching total. Page/loading/data state lives here;
// the caller supplies `fetcher`, which reads whatever filters it needs and
// returns {items, total} (plus any extras, preserved on `extra`).
//
// `filterKey` — a string of all active filter values (same idea as
// usePagination's resetKey). When it changes we snap back to page 1 and
// refetch. Debounce the search term (useDebouncedValue) before folding it into
// filterKey so typing fires one request, not one per keystroke.
//
// Pairs with <Pagination/> (components/Pagination.tsx) for the controls.
export type Page<T> = { items: T[]; total: number };

export function useServerList<T, X = unknown>(
  fetcher: (page: number, pageSize: number) => Promise<Page<T> & X>,
  filterKey: string,
  pageSize = 10,
) {
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<T[]>([]);
  const [total, setTotal] = useState(0);
  const [extra, setExtra] = useState<X | null>(null);
  const [loading, setLoading] = useState(true);

  // Keep the latest fetcher without making it a fetch trigger — only page +
  // filterKey should drive a refetch, not every parent re-render.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // New filters → back to page 1. If already on page 1 the fetch effect below
  // still refires (it depends on filterKey), so no request is missed.
  useEffect(() => { setPage(1); }, [filterKey]);

  const [reloadTick, setReloadTick] = useState(0);
  const reload = useCallback(() => setReloadTick(t => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetcherRef.current(page, pageSize)
      .then(r => {
        if (cancelled) return;
        setItems(r.items ?? []);
        setTotal(r.total ?? 0);
        setExtra(r);
      })
      .catch(() => { if (!cancelled) { setItems([]); setTotal(0); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [page, pageSize, filterKey, reloadTick]);

  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  return { page, setPage, items, total, extra, loading, pageCount, pageSize, reload };
}
