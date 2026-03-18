import { useState, useEffect, useMemo } from 'react';
import { getSessions } from '../api';
import { formatSessionId, displayModel, timeAgo, copyToClipboard } from '../utils';

function CopyBtn({ text }) {
  const [copied, setCopied] = useState(false);
  if (!text) return null;
  return (
    <button className={`copy-btn${copied ? ' copied' : ''}`} onClick={e => {
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

export default function Sessions({ navigate, params = {} }) {
  const [sessions, setSessions] = useState([]);
  const [totalCount, setTotalCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [sortCol, setSortCol] = useState('last_activity');
  const [sortDir, setSortDir] = useState('desc');
  const limit = 20;
  const currentPage = Math.floor((params.offset || 0) / limit) + 1;

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const data = await getSessions(limit, (currentPage - 1) * limit);
        setSessions(data.sessions || []);
        setTotalCount(data.total || data.sessions?.length || 0);
      } catch (e) { setError(e.message); }
      finally { setLoading(false); }
    })();
  }, [currentPage]);

  const totalPages = Math.max(1, Math.ceil(totalCount / limit));

  const filtered = useMemo(() => {
    let list = sessions;
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(s =>
        (s.session_id || '').toLowerCase().includes(q) ||
        (s.model || '').toLowerCase().includes(q) ||
        (s.user || '').toLowerCase().includes(q) ||
        (s.user_question || '').toLowerCase().includes(q)
      );
    }
    list = [...list].sort((a, b) => {
      let av, bv;
      if (sortCol === 'record_count') { av = a.record_count || 0; bv = b.record_count || 0; }
      else if (sortCol === 'model') { av = (a.model || '').toLowerCase(); bv = (b.model || '').toLowerCase(); }
      else { av = a.last_activity || ''; bv = b.last_activity || ''; }
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    return list;
  }, [sessions, search, sortCol, sortDir]);

  const toggleSort = (col) => {
    if (sortCol === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortCol(col); setSortDir('desc'); }
  };

  const SortArrow = ({ col }) => (
    <span className={`sort-arrow${sortCol === col ? ' active' : ''}`}>
      {sortCol === col ? (sortDir === 'asc' ? '▲' : '▼') : '▼'}
    </span>
  );

  if (loading) return <div className="skeleton-block" style={{ height: 400 }} />;
  if (error) return <div className="error-card">Error: {error}</div>;
  if (!sessions.length) return <div className="empty-state"><h3>No sessions found</h3><p>Send requests through the gateway to see audit records here.</p></div>;

  return (
    <div className="fade-child">
      <div className="card" style={{ padding: 0 }}>
        <div style={{ padding: '16px 18px 0' }}>
          <div className="card-head" style={{ marginBottom: 8 }}>
            <span className="card-title">Sessions ({totalCount || sessions.length})</span>
            <Pagination
              current={currentPage}
              total={totalPages}
              onPage={p => navigate('sessions', { offset: (p - 1) * limit })}
            />
          </div>
          <div className="search-bar">
            <input className="search-input" placeholder="Filter by session ID, model, user, or question…" value={search} onChange={e => setSearch(e.target.value)} />
            {search && <span className="search-count">{filtered.length} / {sessions.length}</span>}
          </div>
        </div>
        <div className="table-wrap" style={{ maxHeight: 'none' }}>
          <table style={{ width: '100%' }}>
            <thead>
              <tr>
                <th style={{ width: '38%' }}>Session</th>
                <th className="sortable" onClick={() => toggleSort('record_count')} style={{ width: '8%' }}>Turns <SortArrow col="record_count" /></th>
                <th className="sortable" onClick={() => toggleSort('model')} style={{ width: '14%' }}>Model <SortArrow col="model" /></th>
                <th style={{ width: '22%' }}>Indicators</th>
                <th className="sortable" onClick={() => toggleSort('last_activity')} style={{ width: '18%' }}>Last Active <SortArrow col="last_activity" /></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(s => {
                const tools = s.tool_names ? s.tool_names.split(',').filter(Boolean) : [];
                const isSystemTask = (s.request_type === 'system_task');
                const user = s.user || '';
                const question = s.user_question || '';
                const turns = s.user_message_count || s.record_count || 0;
                const hasRag = s.has_rag_context;
                const hasFiles = s.has_files || s.file_count > 0;

                return (
                  <tr
                    key={s.session_id}
                    className="clickable"
                    onClick={() => navigate('timeline', { sessionId: s.session_id })}
                    style={isSystemTask ? { opacity: 0.5 } : {}}
                  >
                    <td>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                          {user ? (
                            <span style={{ fontSize: 11, color: 'var(--gold)', fontWeight: 600 }}>
                              {'👤 '}{user}
                            </span>
                          ) : isSystemTask ? (
                            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>⚙ system task</span>
                          ) : null}
                          <span className="copy-wrap" style={{ marginLeft: 'auto' }}>
                            <span className="mono" style={{ fontSize: 10, color: 'var(--text-muted)' }}>{formatSessionId(s.session_id)}</span>
                            <CopyBtn text={s.session_id} />
                          </span>
                        </div>
                        {question && (
                          <div style={{
                            fontSize: 13,
                            color: 'var(--text-primary)',
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            maxWidth: '100%',
                            lineHeight: 1.4,
                          }}>
                            "{question.length > 80 ? question.slice(0, 80) + '…' : question}"
                          </div>
                        )}
                      </div>
                    </td>
                    <td style={{ fontFamily: 'var(--mono)', fontWeight: 600, textAlign: 'center' }}>
                      {turns}
                    </td>
                    <td className="mono" style={{ color: 'var(--text-secondary)', fontSize: 12 }}>
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
                            : tools.map(t => [t, 'unknown']);
                          return entries.map(([name, source]) => {
                            let icon, badgeClass;
                            if (source === 'gateway') { icon = name === 'web_search' ? '🔍' : '⚙'; badgeClass = 'badge badge-gold'; }
                            else if (source === 'mcp') { icon = '🔌'; badgeClass = 'badge badge-blue'; }
                            else { icon = '⚙'; badgeClass = 'badge badge-gold'; }
                            return <span key={name} className={badgeClass} style={{ fontSize: 10 }} title={name}>{icon} {name}</span>;
                          });
                        })()}
                        {hasRag && <span className="badge badge-blue" style={{ fontSize: 10 }} title="RAG context detected">📎 RAG</span>}
                        {hasFiles && <span className="badge" style={{ fontSize: 10, background: 'var(--bg-hover)', color: 'var(--text-secondary)' }} title="Files attached">📄 files</span>}
                        {!tools.length && !hasRag && !hasFiles && (
                          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>—</span>
                        )}
                      </div>
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                      {timeAgo(s.last_activity)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div style={{ padding: '12px 18px', display: 'flex', justifyContent: 'center' }}>
          <Pagination
            current={currentPage}
            total={totalPages}
            onPage={p => navigate('sessions', { offset: (p - 1) * limit })}
          />
        </div>
      </div>
    </div>
  );
}
