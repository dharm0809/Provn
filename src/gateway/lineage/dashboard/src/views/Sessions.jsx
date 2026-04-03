import { useState, useEffect, useCallback, useRef } from 'react';
import { getSessions } from '../api';
import { formatSessionId, displayModel, timeAgo, copyToClipboard, formatTime } from '../utils';

function CopyBtn({ text }) {
  const [copied, setCopied] = useState(false);
  if (!text) return null;
  return (
    <button type="button" className={`copy-btn${copied ? ' copied' : ''}`} onClick={e => {
      e.stopPropagation();
      copyToClipboard(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
    }}>{copied ? '✓' : '⎘'}</button>
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
        <tr key={`sk-${i}`} className="sessions-skeleton-row">
          {Array.from({ length: cols }, (_, j) => (
            <td key={j}><div className="skeleton-line skeleton-line-wide" /></td>
          ))}
        </tr>
      ))}
    </>
  );
}

export default function Sessions({ navigate, params = {} }) {
  const limit = 20;
  const offset = Math.max(0, params.offset || 0);
  const qParam = params.q || '';
  const sortCol = params.sort || 'last_activity';
  const sortDir = params.order || 'desc';
  const currentPage = Math.floor(offset / limit) + 1;

  const [sessions, setSessions] = useState([]);
  const [totalCount, setTotalCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [draftQ, setDraftQ] = useState(qParam);
  const [expandedId, setExpandedId] = useState(null);
  const [narrow, setNarrow] = useState(false);
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;

  useEffect(() => { setDraftQ(qParam); }, [qParam]);

  useEffect(() => {
    const mq = window.matchMedia('(max-width: 900px)');
    const fn = () => setNarrow(mq.matches);
    fn();
    mq.addEventListener('change', fn);
    return () => mq.removeEventListener('change', fn);
  }, []);

  useEffect(() => {
    const t = setTimeout(() => {
      const trimmed = draftQ.trim();
      const cur = (qParam || '').trim();
      if (trimmed === cur) return;
      navigateRef.current('sessions', {
        offset: 0, q: draftQ, sort: sortCol, order: sortDir,
      });
    }, 350);
    return () => clearTimeout(t);
  }, [draftQ, qParam, sortCol, sortDir]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getSessions(limit, offset, { q: qParam, sort: sortCol, order: sortDir });
      setSessions(data.sessions || []);
      setTotalCount(Number(data.total) || 0);
    } catch (e) {
      setError(e.message || String(e));
      setSessions([]);
      setTotalCount(0);
    } finally {
      setLoading(false);
    }
  }, [limit, offset, qParam, sortCol, sortDir, reloadKey]);

  useEffect(() => { load(); }, [load]);

  const retry = () => setReloadKey(k => k + 1);

  const totalPages = Math.max(1, Math.ceil(totalCount / limit));
  const rangeStart = totalCount === 0 ? 0 : offset + 1;
  const rangeEnd = offset + sessions.length;

  const goSessions = (next) => {
    navigate('sessions', {
      offset: next.offset ?? offset,
      q: next.q != null ? next.q : qParam,
      sort: next.sort != null ? next.sort : sortCol,
      order: next.order != null ? next.order : sortDir,
    });
  };

  const applySort = (col) => {
    const nextOrder = sortCol === col ? (sortDir === 'asc' ? 'desc' : 'asc') : 'desc';
    goSessions({ offset: 0, sort: col, order: nextOrder });
  };

  const ariaSortFor = (col) => {
    if (sortCol !== col) return 'none';
    return sortDir === 'asc' ? 'ascending' : 'descending';
  };

  const openTimeline = (sessionId) => {
    navigate('timeline', { sessionId });
  };

  const SortBtn = ({ col, label, width }) => (
    <th scope="col" aria-sort={ariaSortFor(col)} style={width ? { width } : undefined}>
      <button
        type="button"
        className="th-sort-btn"
        onClick={() => applySort(col)}
      >
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
          <p><strong>Could not load sessions.</strong> {error}</p>
          <button type="button" className="btn btn-primary" onClick={retry}>Retry</button>
        </div>
      </div>
    );
  }

  const noRows = !loading && sessions.length === 0;
  const emptyHint = (qParam || '').trim()
    ? 'No sessions match your search. Try different keywords or clear the filter.'
    : 'Send requests through the gateway to see audit records here.';

  return (
    <div className="fade-child">
      <div className="card sessions-card" style={{ padding: 0 }}>
        <div style={{ padding: '16px 18px 0' }}>
          <div className="card-head" style={{ marginBottom: 8, flexWrap: 'wrap', gap: 12 }}>
            <div>
              <span className="card-title">Sessions ({totalCount})</span>
              {totalCount > 0 && (
                <span className="sessions-range muted" style={{ marginLeft: 12, fontSize: 13 }}>
                  Showing {rangeStart}–{rangeEnd} of {totalCount}
                </span>
              )}
            </div>
            <Pagination
              current={currentPage}
              total={totalPages}
              onPage={p => goSessions({ offset: (p - 1) * limit })}
            />
          </div>
          <div className="search-bar">
            <label htmlFor="sessions-search" className="sr-only">Search sessions</label>
            <input
              id="sessions-search"
              className="search-input"
              placeholder="Search by session ID, model, user, or question…"
              value={draftQ}
              onChange={e => setDraftQ(e.target.value)}
              autoComplete="off"
            />
            {loading && <span className="search-count sessions-loading-hint" aria-live="polite">Updating…</span>}
          </div>
        </div>
        <div className="table-wrap sessions-table-wrap" style={{ maxHeight: 'none' }}>
          <table className="sessions-table" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th scope="col" style={{ width: '38%' }}>Session</th>
                <SortBtn col="record_count" label="Turns" width="8%" />
                <SortBtn col="model" label="Model" width="14%" />
                <th scope="col" style={{ width: '22%' }}>Indicators</th>
                <SortBtn col="last_activity" label="Last active" width="18%" />
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <TableSkeletonRows cols={5} />
              ) : noRows ? (
                <tr className="sessions-empty-row">
                  <td colSpan={5} className="empty-state-cell">
                    <div className="empty-state compact">
                      <h3>No sessions found</h3>
                      <p>{emptyHint}</p>
                    </div>
                  </td>
                </tr>
              ) : (
                sessions.map(s => {
                  const tools = s.tool_names ? s.tool_names.split(',').filter(Boolean) : [];
                  const isSystemTask = (s.request_type === 'system_task');
                  const user = s.user || '';
                  const question = s.user_question || '';
                  const turns = s.user_message_count || s.record_count || 0;
                  const hasRag = s.has_rag_context;
                  const hasFiles = s.has_files || s.file_count > 0;
                  const expanded = expandedId === s.session_id;
                  const qPreviewClass = narrow && question
                    ? `sessions-q-preview${expanded ? ' is-expanded' : ''}`
                    : 'sessions-q-preview';

                  return (
                    <tr
                      key={s.session_id}
                      className="clickable sessions-row"
                      tabIndex={0}
                      role="row"
                      onClick={() => openTimeline(s.session_id)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          openTimeline(s.session_id);
                        }
                      }}
                      style={isSystemTask ? { opacity: 0.5 } : {}}
                    >
                      <td>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                            {user ? (
                              <span style={{ fontSize: 13, color: 'var(--gold)', fontWeight: 600 }}>
                                {'👤 '}{user}
                              </span>
                            ) : isSystemTask ? (
                              <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>⚙ system task</span>
                            ) : null}
                            <span className="copy-wrap" style={{ marginLeft: 'auto' }}>
                              <span className="mono" style={{ fontSize: 12, color: 'var(--text-muted)' }}>{formatSessionId(s.session_id)}</span>
                              <CopyBtn text={s.session_id} />
                            </span>
                          </div>
                          {question && (
                            <div className="sessions-q-block">
                              {narrow && (
                                <button
                                  type="button"
                                  className="sessions-q-toggle"
                                  aria-expanded={expanded}
                                  aria-label={expanded ? 'Collapse question' : 'Expand question'}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setExpandedId(expanded ? null : s.session_id);
                                  }}
                                >
                                  {expanded ? '▾' : '▸'}
                                </button>
                              )}
                              <div
                                className={qPreviewClass}
                                title={!narrow ? `"${question}"` : undefined}
                              >
                                &ldquo;{!narrow && question.length > 80 ? `${question.slice(0, 80)}…` : question}&rdquo;
                              </div>
                            </div>
                          )}
                        </div>
                      </td>
                      <td style={{ fontFamily: 'var(--mono)', fontWeight: 600, textAlign: 'center' }}>
                        {turns}
                      </td>
                      <td className="mono" style={{ color: 'var(--text-secondary)', fontSize: 14 }}>
                        {displayModel(s.model)}
                      </td>
                      <td>
                        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
                          {tools.length > 0 && (() => {
                            const details = s.tool_details ? s.tool_details.split(',').filter(Boolean) : [];
                            const toolMap = {};
                            details.forEach(d => {
                              const [name, source] = d.split(':');
                              if (name) toolMap[name] = source || 'unknown';
                            });
                            const entries = Object.keys(toolMap).length > 0
                              ? Object.entries(toolMap)
                              : tools.map(tn => [tn, 'unknown']);
                            return entries.map(([name, source]) => {
                              let icon;
                              let badgeClass;
                              if (source === 'gateway') { icon = name === 'web_search' ? '🔍' : '⚙'; badgeClass = 'badge badge-gold'; }
                              else if (source === 'mcp') { icon = '🔌'; badgeClass = 'badge badge-blue'; }
                              else { icon = '⚙'; badgeClass = 'badge badge-gold'; }
                              return <span key={name} className={badgeClass} style={{ fontSize: 12 }} title={name}>{icon} {name}</span>;
                            });
                          })()}
                          {hasRag && <span className="badge badge-blue" style={{ fontSize: 12 }} title="RAG context detected">📎 RAG</span>}
                          {hasFiles && <span className="badge" style={{ fontSize: 10, background: 'var(--bg-hover)', color: 'var(--text-secondary)' }} title="Files attached">📄 files</span>}
                          {!tools.length && !hasRag && !hasFiles && (
                            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>—</span>
                          )}
                        </div>
                      </td>
                      <td
                        style={{ fontSize: 13, color: 'var(--text-muted)' }}
                        title={s.last_activity ? formatTime(s.last_activity) : undefined}
                      >
                        {timeAgo(s.last_activity)}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
        <div style={{ padding: '12px 18px', display: 'flex', justifyContent: 'center' }}>
          <Pagination
            current={currentPage}
            total={totalPages}
            onPage={p => goSessions({ offset: (p - 1) * limit })}
          />
        </div>
      </div>
    </div>
  );
}
