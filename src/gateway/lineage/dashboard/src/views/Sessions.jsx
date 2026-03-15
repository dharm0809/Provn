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

export default function Sessions({ navigate, params = {} }) {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [sortCol, setSortCol] = useState('last_activity');
  const [sortDir, setSortDir] = useState('desc');
  const limit = 50;
  const offset = params.offset || 0;

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const data = await getSessions(limit, offset);
        setSessions(data.sessions || []);
      } catch (e) { setError(e.message); }
      finally { setLoading(false); }
    })();
  }, [offset]);

  const filtered = useMemo(() => {
    let list = sessions;
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(s =>
        (s.session_id || '').toLowerCase().includes(q) ||
        (s.model || '').toLowerCase().includes(q)
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
      <div className="card">
        <div className="card-head">
          <span className="card-title">All Sessions ({sessions.length})</span>
        </div>
        <div style={{ padding: '0 16px' }}>
          <div className="search-bar">
            <input className="search-input" placeholder="Filter by session ID or model…" value={search} onChange={e => setSearch(e.target.value)} />
            {search && <span className="search-count">{filtered.length} / {sessions.length}</span>}
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Session</th>
                <th className="sortable" onClick={() => toggleSort('record_count')}>Records <SortArrow col="record_count" /></th>
                <th className="sortable" onClick={() => toggleSort('model')}>Model <SortArrow col="model" /></th>
                <th>Tools</th>
                <th className="sortable" onClick={() => toggleSort('last_activity')}>Last Active <SortArrow col="last_activity" /></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(s => {
                const tools = s.tool_names ? s.tool_names.split(',').filter(Boolean) : [];
                return (
                  <tr key={s.session_id} className="clickable" onClick={() => navigate('timeline', { sessionId: s.session_id })}>
                    <td>
                      <div className="copy-wrap">
                        <span className="copy-text id">{formatSessionId(s.session_id)}</span>
                        <CopyBtn text={s.session_id} />
                      </div>
                    </td>
                    <td style={{ fontFamily: 'var(--mono)', fontWeight: 600 }}>{s.record_count}</td>
                    <td className="mono" style={{ color: 'var(--text-secondary)' }}>{displayModel(s.model)}</td>
                    <td>
                      {tools.length > 0 ? (
                        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                          {(() => {
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
                              let icon, badgeClass, label;
                              if (source === 'gateway') { icon = name === 'web_search' ? '🔍' : '⚙'; badgeClass = 'badge badge-gold'; label = 'Built-in'; }
                              else if (source === 'mcp') { icon = '🔌'; badgeClass = 'badge badge-blue'; label = 'MCP'; }
                              else if (source === 'provider') { icon = '🌐'; badgeClass = 'badge badge-green'; label = 'External'; }
                              else { icon = '⚙'; badgeClass = 'badge badge-gold'; label = 'Tool'; }
                              return <span key={name} className={badgeClass} style={{ fontSize: 10 }} title={`${label}: ${name}`}>{icon} {name}</span>;
                            });
                          })()}
                        </div>
                      ) : (
                        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>—</span>
                      )}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{timeAgo(s.last_activity)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {sessions.length >= limit && (
          <div style={{ display: 'flex', justifyContent: 'center', gap: 8, marginTop: 16 }}>
            {offset > 0 && <button className="btn" onClick={() => navigate('sessions', { offset: offset - limit })}>Previous</button>}
            <button className="btn" onClick={() => navigate('sessions', { offset: offset + limit })}>Next</button>
          </div>
        )}
      </div>
    </div>
  );
}
