import { useState, useEffect } from 'react';
import { getSessions } from '../api';
import { formatSessionId, displayModel, timeAgo } from '../utils';

export default function Sessions({ navigate, params = {} }) {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
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

  if (loading) return <div className="skeleton-block" style={{ height: 400 }} />;
  if (error) return <div className="error-card">Error: {error}</div>;
  if (!sessions.length) return <div className="empty-state"><h3>No sessions found</h3><p>Send requests through the gateway to see audit records here.</p></div>;

  return (
    <div className="fade-child">
      <div className="card">
        <div className="card-head">
          <span className="card-title">All Sessions ({sessions.length})</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Session</th><th>Records</th><th>Model</th><th>Tools</th><th>Last Active</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map(s => {
                const tools = s.tool_names ? s.tool_names.split(',').filter(Boolean) : [];
                return (
                  <tr key={s.session_id} className="clickable" onClick={() => navigate('timeline', { sessionId: s.session_id })}>
                    <td className="id">{formatSessionId(s.session_id)}</td>
                    <td style={{ fontFamily: 'var(--mono)', fontWeight: 600 }}>{s.record_count}</td>
                    <td className="mono" style={{ color: 'var(--text-secondary)' }}>{displayModel(s.model)}</td>
                    <td>
                      {tools.length > 0 ? (
                        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                          {(() => {
                            // Parse tool_details "name:source,name:source" into categorized groups
                            const details = s.tool_details ? s.tool_details.split(',').filter(Boolean) : [];
                            const toolMap = {};
                            details.forEach(d => {
                              const [name, source] = d.split(':');
                              if (name) toolMap[name] = source || 'unknown';
                            });
                            // Fall back to tool_names if no details
                            const entries = Object.keys(toolMap).length > 0
                              ? Object.entries(toolMap)
                              : tools.map(t => [t, 'unknown']);
                            return entries.map(([name, source]) => {
                              let icon, badgeClass, label;
                              if (source === 'gateway') {
                                icon = name === 'web_search' ? '🔍' : '⚙';
                                badgeClass = 'badge badge-gold';
                                label = 'Built-in';
                              } else if (source === 'mcp') {
                                icon = '🔌';
                                badgeClass = 'badge badge-blue';
                                label = 'MCP';
                              } else if (source === 'provider') {
                                icon = '🌐';
                                badgeClass = 'badge badge-green';
                                label = 'External';
                              } else {
                                icon = '⚙';
                                badgeClass = 'badge badge-gold';
                                label = 'Tool';
                              }
                              return (
                                <span key={name} className={badgeClass} style={{ fontSize: 10 }} title={`${label}: ${name}`}>
                                  {icon} {name}
                                </span>
                              );
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
