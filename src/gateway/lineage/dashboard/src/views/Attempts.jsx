import { useState, useEffect, useCallback, useRef } from 'react';
import { getAttempts } from '../api';
import {
  displayModel, timeAgo, dispositionClass, dispositionLabel, dispositionSummaryLabel,
  statusCodeClass, formatTime, truncId, formatNumber,
} from '../utils';

/** Summary for this Attempts tab only: same `q` filter as `/v1/lineage/attempts` (not Overview or Sessions). */
function AttemptsSummaryStrip({
  total, stats, loading, qParam, rangeStart, rangeEnd,
}) {
  const divider = <div style={{ width: 1, height: 18, background: 'var(--border)', flexShrink: 0 }} />;
  const statKeysSorted = Object.keys(stats).sort((a, b) => {
    const order = ['forwarded', 'allowed', 'denied_auth', 'denied_policy', 'denied_attestation', 'error'];
    const ia = order.indexOf(a);
    const ib = order.indexOf(b);
    if (ia >= 0 && ib >= 0) return ia - ib;
    if (ia >= 0) return -1;
    if (ib >= 0) return 1;
    return a.localeCompare(b);
  });
  const allowedish = (stats.forwarded || 0) + (stats.allowed || 0);
  const pctAllowed = total > 0 ? ((allowedish / total) * 100).toFixed(1) : '—';
  const hasFilter = Boolean((qParam || '').trim());
  const scopeHint = hasFilter
    ? 'Filtered attempts on this tab · counts include all pages of this result'
    : 'Attempts on this tab only · counts include all pages of this list';

  return (
    <div className="card card-accent-green" style={{ padding: '12px 20px', marginBottom: 16 }}>
      <div style={{ marginBottom: 10 }}>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 700, color: 'var(--gold)', letterSpacing: '0.5px' }}>
          ATTEMPTS · THIS TAB
        </div>
        <div className="attempts-scope-hint" style={{ fontSize: 10, color: 'var(--text-secondary)', marginTop: 4, lineHeight: 1.4, maxWidth: 640 }}>
          {scopeHint}
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 20, flexWrap: 'wrap' }}>
        <div style={{ fontSize: 12, color: 'var(--text-secondary)', fontFamily: 'var(--mono)', whiteSpace: 'nowrap' }}>
          <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>
            {loading ? '…' : formatNumber(total)}
          </span>
          <span style={{ margin: '0 5px', opacity: 0.3 }}>·</span>
          <span style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.4px' }} title="Rows matching your search on this tab (all pages)">rows in view</span>
        </div>

        {divider}

        {total > 0 && rangeStart > 0 ? (
          <div style={{ fontSize: 12, color: 'var(--text-secondary)', fontFamily: 'var(--mono)', whiteSpace: 'nowrap' }}>
            <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{rangeStart}–{rangeEnd}</span>
            <span style={{ margin: '0 5px', opacity: 0.3 }}>·</span>
            <span style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.4px' }}>page slice</span>
          </div>
        ) : null}

        {(total > 0 && rangeStart > 0) ? divider : null}

        <div style={{ fontSize: 12, color: 'var(--text-secondary)', fontFamily: 'var(--mono)', whiteSpace: 'nowrap' }}>
          <span style={{ color: 'var(--green)', fontWeight: 600 }}>{pctAllowed === '—' ? '—' : `${pctAllowed}%`}</span>
          <span style={{ margin: '0 5px', opacity: 0.3 }}>·</span>
          <span style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.4px' }}>allowed or forwarded (of view)</span>
        </div>

        {statKeysSorted.length > 0 && divider}

        {statKeysSorted.map((k) => {
          const isGood = k === 'forwarded' || k === 'allowed';
          const isBad = k.startsWith('denied');
          const color = isGood ? 'var(--green)' : isBad ? 'var(--red)' : 'var(--amber)';
          const pct = total > 0 ? `${((stats[k] / total) * 100).toFixed(0)}%` : '—';
          return (
            <div key={k} style={{ fontSize: 12, color: 'var(--text-secondary)', fontFamily: 'var(--mono)', whiteSpace: 'nowrap' }} title={k}>
              <span style={{ color, fontWeight: 600 }}>{stats[k]}</span>
              <span style={{ margin: '0 5px', opacity: 0.3 }}>·</span>
              <span style={{ fontSize: 11, letterSpacing: '0.2px', fontFamily: 'var(--font)' }}>{dispositionSummaryLabel(k)}</span>
              <span style={{ margin: '0 4px', opacity: 0.25 }}>/</span>
              <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{pct} of view</span>
            </div>
          );
        })}

        {hasFilter ? (
          <>
            {divider}
            <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--mono)', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis' }} title={(qParam || '').trim()}>
              <span style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.4px', marginRight: 6 }}>search</span>
              {(qParam || '').trim()}
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

function Pagination({ current, total, onPage }) {
  if (total <= 1) return null;
  const pages = [];
  const maxVisible = 7;

  if (total <= maxVisible) {
    for (let i = 1; i <= total; i++) pages.push(i);
  } else {
    pages.push(1);
    let start = Math.max(2, current - 1);
    let end = Math.min(total - 1, current + 1);
    if (current <= 3) { start = 2; end = 5; }
    if (current >= total - 2) { start = total - 4; end = total - 1; }
    if (start > 2) pages.push('...');
    for (let i = start; i <= end; i++) pages.push(i);
    if (end < total - 1) pages.push('...');
    pages.push(total);
  }

  return (
    <div className="pagination">
      {pages.map((p, i) =>
        p === '...' ? (
          <span key={`ellipsis-${i}`} className="pagination-ellipsis">…</span>
        ) : (
          <button
            key={p}
            type="button"
            className={`pagination-btn${p === current ? ' active' : ''}`}
            onClick={() => onPage(p)}
          >
            {p}
          </button>
        )
      )}
    </div>
  );
}

function TableSkeletonRows({ cols }) {
  return (
    <>
      {Array.from({ length: 8 }, (_, i) => (
        <tr key={`att-sk-${i}`} className="sessions-skeleton-row">
          {Array.from({ length: cols }, (_, j) => (
            <td key={j}><div className="skeleton-line skeleton-line-wide" /></td>
          ))}
        </tr>
      ))}
    </>
  );
}

const SORT_COLS = ['timestamp', 'disposition', 'request_id', 'user', 'model_id', 'path', 'status_code'];

export default function Attempts({ navigate, params = {} }) {
  const limit = 100;
  const offset = Math.max(0, params.offset || 0);
  const qParam = params.q || '';
  const sortCol = SORT_COLS.includes(params.sort) ? params.sort : 'timestamp';
  const sortDir = params.order || 'desc';
  const currentPage = Math.floor(offset / limit) + 1;

  const [items, setItems] = useState([]);
  const [stats, setStats] = useState({});
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [draftQ, setDraftQ] = useState(qParam);
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;

  useEffect(() => { setDraftQ(qParam); }, [qParam]);

  useEffect(() => {
    const t = setTimeout(() => {
      const trimmed = draftQ.trim();
      const cur = (qParam || '').trim();
      if (trimmed === cur) return;
      navigateRef.current('attempts', {
        offset: 0, q: draftQ, sort: sortCol, order: sortDir,
      });
    }, 350);
    return () => clearTimeout(t);
  }, [draftQ, qParam, sortCol, sortDir]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getAttempts(limit, offset, { q: qParam, sort: sortCol, order: sortDir });
      setItems(data.items || []);
      setStats(data.stats || {});
      setTotal(Number(data.total) || 0);
    } catch (e) {
      setError(e.message || String(e));
      setItems([]);
      setStats({});
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [limit, offset, qParam, sortCol, sortDir, reloadKey]);

  useEffect(() => { load(); }, [load]);

  const retry = () => setReloadKey(k => k + 1);

  const totalPages = Math.max(1, Math.ceil(total / limit));
  const rangeStart = total === 0 ? 0 : offset + 1;
  const rangeEnd = offset + items.length;

  const goAttempts = (next) => {
    navigate('attempts', {
      offset: next.offset ?? offset,
      q: next.q != null ? next.q : qParam,
      sort: next.sort != null ? next.sort : sortCol,
      order: next.order != null ? next.order : sortDir,
    });
  };

  const applySort = (col) => {
    const nextOrder = sortCol === col ? (sortDir === 'asc' ? 'desc' : 'asc') : 'desc';
    goAttempts({ offset: 0, sort: col, order: nextOrder });
  };

  const ariaSortFor = (col) => {
    if (sortCol !== col) return 'none';
    return sortDir === 'asc' ? 'ascending' : 'descending';
  };

  const openExecution = (executionId) => {
    if (executionId) navigate('execution', { executionId });
  };

  const SortBtn = ({ col, label, width }) => (
    <th scope="col" aria-sort={ariaSortFor(col)} style={width ? { width } : undefined}>
      <button type="button" className="th-sort-btn" onClick={() => applySort(col)}>
        <span>{label}</span>
        <span className={`sort-arrow${sortCol === col ? ' active' : ''}`} aria-hidden>
          {sortCol === col ? (sortDir === 'asc' ? '▲' : '▼') : '▼'}
        </span>
      </button>
    </th>
  );

  if (error && !loading) {
    return (
      <div className="fade-child">
        <div className="error-card" role="alert">
          <p><strong>Could not load attempts.</strong> {error}</p>
          <button type="button" className="btn btn-primary" onClick={retry}>Retry</button>
        </div>
      </div>
    );
  }

  const noRows = !loading && items.length === 0;
  const emptyHint = (qParam || '').trim()
    ? 'No attempts match your search. Try different keywords or clear the filter.'
    : 'Completeness and proxy traffic will appear here as gateway_attempts rows.';

  return (
    <div className="fade-child">
      <AttemptsSummaryStrip
        total={total}
        stats={stats}
        loading={loading}
        qParam={qParam}
        rangeStart={rangeStart}
        rangeEnd={rangeEnd}
      />

      <div className="card" style={{ padding: 0 }}>
        <div style={{ padding: '16px 18px 0' }}>
          <div className="card-head" style={{ marginBottom: 8, flexWrap: 'wrap', gap: 12 }}>
            <div>
              <span className="card-title">Attempts</span>
              {total > 0 && (
                <span className="sessions-range muted" style={{ marginLeft: 12, fontSize: 13 }}>
                  Showing {rangeStart}–{rangeEnd} of {total}
                </span>
              )}
            </div>
            <Pagination current={currentPage} total={totalPages} onPage={p => goAttempts({ offset: (p - 1) * limit })} />
          </div>
          <div className="search-bar">
            <label htmlFor="attempts-search" className="sr-only">Search attempts</label>
            <input
              id="attempts-search"
              className="search-input"
              placeholder="Search request id, user, model, path, disposition, provider, execution id, status…"
              value={draftQ}
              onChange={e => setDraftQ(e.target.value)}
              autoComplete="off"
            />
            {loading && <span className="sessions-loading-hint" aria-live="polite">Updating…</span>}
          </div>
        </div>

        <div className="table-wrap sessions-table-wrap">
          <table className="sessions-table" style={{ width: '100%' }}>
            <thead>
              <tr>
                <SortBtn col="disposition" label="Disposition" width="12%" />
                <SortBtn col="request_id" label="Request" width="14%" />
                <SortBtn col="user" label="User" width="10%" />
                <SortBtn col="model_id" label="Model" width="12%" />
                <SortBtn col="path" label="Path" width="18%" />
                <SortBtn col="status_code" label="Status" width="8%" />
                <SortBtn col="timestamp" label="Time" width="12%" />
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <TableSkeletonRows cols={7} />
              ) : noRows ? (
                <tr className="sessions-empty-row">
                  <td colSpan={7} className="empty-state-cell">
                    <div className="empty-state compact">
                      <h3>No attempts recorded</h3>
                      <p>{emptyHint}</p>
                    </div>
                  </td>
                </tr>
              ) : (
                items.map(a => (
                  <tr
                    key={a.request_id || `${a.timestamp}-${a.path}`}
                    className={a.execution_id ? 'clickable sessions-row' : 'sessions-row'}
                    tabIndex={a.execution_id ? 0 : undefined}
                    role="row"
                    onClick={() => openExecution(a.execution_id)}
                    onKeyDown={(e) => {
                      if (!a.execution_id) return;
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        openExecution(a.execution_id);
                      }
                    }}
                  >
                    <td><span className={`badge ${dispositionClass(a.disposition)}`}>{dispositionLabel(a.disposition)}</span></td>
                    <td className="mono" style={{ fontSize: 12, color: 'var(--text-muted)' }} title={a.request_id || ''}>
                      {truncId(a.request_id, 14) || '—'}
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>{a.user || '-'}</td>
                    <td className="mono" style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{displayModel(a.model_id) || '-'}</td>
                    <td className="mono" style={{ fontSize: 12, color: 'var(--text-muted)' }}>{a.path}</td>
                    <td><span className={`badge ${statusCodeClass(a.status_code)}`}>{a.status_code}</span></td>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }} title={a.timestamp ? formatTime(a.timestamp) : undefined}>
                      {timeAgo(a.timestamp)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        <div style={{ padding: '12px 18px', display: 'flex', justifyContent: 'center' }}>
          <Pagination current={currentPage} total={totalPages} onPage={p => goAttempts({ offset: (p - 1) * limit })} />
        </div>
      </div>
    </div>
  );
}
