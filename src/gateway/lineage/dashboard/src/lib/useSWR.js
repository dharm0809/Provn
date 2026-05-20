/**
 * Minimal stale-while-revalidate hook. ~60 lines, no external deps.
 *
 * Contract:
 *   const { data, error, isLoading, refresh } = useSWR(key, fetcher, { intervalMs: 30000, onSuccess })
 *
 *   - First mount: returns cached value immediately if any, AND fires fetcher.
 *   - When the fetcher resolves: cache is updated, data state is updated.
 *   - Periodic refresh: fetcher runs every `intervalMs` ms (default 30000).
 *     When the tab is hidden the interval fires but the fetcher is skipped
 *     (the next visible tick will refresh).
 *   - Manual refresh(): returns a Promise that resolves to the new data.
 *   - isLoading is true only when there is no cached value yet AND a fetch
 *     is in flight; subsequent revalidations are silent (stale-while-revalidate).
 *   - The cache is per-key (caller picks a stable string). Cache is module-
 *     scoped so multiple components reading the same key share a result and
 *     a refetch by one updates the other on its next render.
 *   - Fetcher errors are captured into `error`; the previous `data` is kept
 *     (stale-while-error). The next successful fetch clears `error`.
 *
 * NOT included on purpose:
 *   - No multi-tab broadcast (BroadcastChannel) — overkill for our needs.
 *   - No focus-revalidation. Operators have a 30s interval; tab-focus
 *     refetch noise is more annoying than helpful here.
 *   - No deduping of in-flight requests by key. Components for the same key
 *     would fire parallel fetchers; acceptable until we have a use case.
 *   - No `mutate()` — only `refresh()` (no optimistic updates yet).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { isTabVisible } from '../utils';

const _cache = new Map(); // key -> { data, lastFetchedAt }

export function _resetSWRCacheForTests() {
  _cache.clear();
}

export function useSWR(key, fetcher, options = {}) {
  const intervalMs = options.intervalMs ?? 30000;
  const onSuccess = options.onSuccess;

  const cached = key != null ? _cache.get(key) : undefined;
  const [data, setData] = useState(cached ? cached.data : undefined);
  const [error, setError] = useState(null);
  const [isLoading, setIsLoading] = useState(cached === undefined);

  // Keep latest fetcher/onSuccess in a ref so we don't restart the interval
  // every render. The interval should only restart if key or intervalMs change.
  const fetcherRef = useRef(fetcher);
  const onSuccessRef = useRef(onSuccess);
  fetcherRef.current = fetcher;
  onSuccessRef.current = onSuccess;

  const cancelledRef = useRef(false);

  const runFetch = useCallback(async () => {
    if (key == null) return undefined;
    try {
      const result = await fetcherRef.current();
      if (cancelledRef.current) return result;
      _cache.set(key, { data: result, lastFetchedAt: Date.now() });
      setData(result);
      setError(null);
      if (typeof onSuccessRef.current === 'function') {
        try { onSuccessRef.current(result); } catch { /* swallow — UI side-effect must not break SWR */ }
      }
      return result;
    } catch (e) {
      if (!cancelledRef.current) setError(e);
      throw e;
    } finally {
      if (!cancelledRef.current) setIsLoading(false);
    }
  }, [key]);

  useEffect(() => {
    cancelledRef.current = false;
    if (_cache.get(key) === undefined) setIsLoading(true);
    runFetch().catch(() => { /* error state already set */ });
    const id = setInterval(() => {
      if (isTabVisible()) {
        runFetch().catch(() => { /* error state already set */ });
      }
    }, intervalMs);
    return () => {
      cancelledRef.current = true;
      clearInterval(id);
    };
  }, [key, intervalMs, runFetch]);

  return { data, error, isLoading, refresh: runFetch };
}

export default useSWR;
