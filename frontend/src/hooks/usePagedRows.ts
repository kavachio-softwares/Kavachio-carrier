// Paging for a list that is already in the browser (a table the server sends
// whole): the rows of the page shown, and the props <Pagination> takes. Goes
// back to page 1 when the list is replaced (new filter, reload), and never
// sits past the last page when rows go away.
import { useEffect, useState } from "react";

export const AUDIT_PAGE = 20;

export function usePagedRows<T>(rows: T[], pageSize = AUDIT_PAGE, resetKey?: unknown) {
  const [page, setPage] = useState(1);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  useEffect(() => { setPage(1); }, [resetKey]);
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);
  const at = Math.min(page, pageCount);
  return {
    rows: rows.slice((at - 1) * pageSize, at * pageSize),
    pager: { page: at, pageCount, pageSize, totalItems: rows.length, onPageChange: setPage },
  };
}
