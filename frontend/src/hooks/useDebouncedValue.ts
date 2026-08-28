import { useEffect, useState } from "react";

// Delays reflecting a fast-changing value (typically a search box) until it
// pauses for `delay`ms — for a server-side search, that's the difference
// between one request and one per keystroke.
export function useDebouncedValue<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}
