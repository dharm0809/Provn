import { useState, useEffect, useMemo } from 'react';
import { getAttempts } from '../api';
import { displayModel, timeAgo, formatNumber, dispositionClass, dispositionLabel, statusCodeClass } from '../utils';

export default function Attempts({ navigate, params = {} }) {
  const [items, setItems] = useState([]);
  const [stats, setStats] = useState({});
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [sortCol, setSortCol] = useState('timestamp');
  const [sortDir, setSortDir] = useState('desc');
  const limit = 100;
  const offset = params.offset || 0;

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const data = await getAttempts(limit, offset);
        setItems(data.items || []);
        setStats(data.stats || {});
        setTotal(data.total || 0);
      } catch (e) { setError(e.message); }
      finally { setLoading(false); }
    })();
  }, [offset]);

  const toggleSort = (col) => {
    if (sortCol === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortCol(col); setSortDir('desc'); }
  };

  const SortArrow = ({ col }) => (
    <span className={`sort-arrow${sortCol === col ? ' active' : ''}`}>
      {sortCol === col ? (sortDir === 'asc' ? '▲' : '▼') : '▼'}
    </span>
  );

  const filtered = useMemo(() => {
    let list = items;
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(a =>
        (a.disposition || '').toLowerCase().includes(q) ||
        (a.user || '').toLowerCase().includes(q) ||
        (a.model_id || '').toLowerCase().includes(q) ||
        (a.path || '').toLowerCase().includes(q)
      );
    }
    list = [...list].sort((a, b) => {
      let av, bv;
      if (sortCol === 'disposition') { av = (a.disposition || '').toLowerCase(); bv = (b.disposition || '').toLowerCase(); }
      else if (sortCol === 'user') { av = (a.user || '').toLowerCase(); bv = (b.user || '').toLowerCase(); }
      else if (sortCol === 'model_id') { av = (a.model_id || '').toLowerCase(); bv = (b.model_id || '').toLowerCase(); }
      else if (sortCol === 'path') { av = (a.path || '').toLowerCase(); bv = (b.path || '').toLowerCase(); }
      else if (sortCol === 'status_code') { av = a.status_code || 0; bv = b.status_code || 0; }
      else { av = a.timestamp || ''; bv = b.timestamp || ''; }
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    return list;
  }, [items, search, sortCol, sortDir]);

  if (loading) return <div className="skeleton-block" style={{ height: 400 }} />;
  if (error) return <div className="error-card">Error: {error}</div>;

  const statKeys = Object.keys(stats);

  return (
    <div className="fade-child">
      {/* Stats */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 12, marginBottom: 16 }}>
        <div className="stat-card">
          <div className="stat-value">{total}</div>
          <div className="stat-label">Total Attempts</div>
        </div>
        {statKeys.map(k => {
          const color = k === 'allowed' || k === 'forwarded' ? 'var(--green)' : k.startsWith('denied') ? 'var(--red)' : 'var(--amber)';
          return (
            <div key={k} className="stat-card">
              <div className="stat-value">{stats[k]}</div>
              <div className="stat-label">{k.replace(/_/g, ' ')}</div>
              <div className="stat-sub" style={{ color }}>{(stats[k] / total * 100).toFixed(0)}%</div>
            </div>
          );
        })}
      </div>

      {items.length === 0 ? (
        <div className="empty-state"><h3>No attempts recorded</h3></div>
      ) : (
        <div className="card">
          <div className="card-head">
            <span className="card-title">Attempts</span>
          </div>
          <div style={{ padding: '0 16px' }}>
            <div className="search-bar">
              <input className="search-input" placeholder="Filter by disposition, user, model, or path…" value={search} onChange={e => setSearch(e.target.value)} />
              {search && <span className="search-count">{filtered.length} / {items.length}</span>}
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="sortable" onClick={() => toggleSort('disposition')}>Disposition <SortArrow col="disposition" /></th>
                  <th className="sortable" onClick={() => toggleSort('user')}>User <SortArrow col="user" /></th>
                  <th className="sortable" onClick={() => toggleSort('model_id')}>Model <SortArrow col="model_id" /></th>
                  <th className="sortable" onClick={() => toggleSort('path')}>Path <SortArrow col="path" /></th>
                  <th className="sortable" onClick={() => toggleSort('status_code')}>Status <SortArrow col="status_code" /></th>
                  <th className="sortable" onClick={() => toggleSort('timestamp')}>Time <SortArrow col="timestamp" /></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((a, i) => (
                  <tr key={i} className={a.execution_id ? 'clickable' : ''} onClick={() => a.execution_id && navigate('execution', { executionId: a.execution_id })}>
                    <td><span className={`badge ${dispositionClass(a.disposition)}`}>{dispositionLabel(a.disposition)}</span></td>
                    <td className="mono" style={{ fontSize: 12 }}>{a.user || '-'}</td>
                    <td className="mono" style={{ color: 'var(--text-secondary)' }}>{displayModel(a.model_id) || '-'}</td>
                    <td className="mono" style={{ fontSize: 12, color: 'var(--text-muted)' }}>{a.path}</td>
                    <td><span className={`badge ${statusCodeClass(a.status_code)}`}>{a.status_code}</span></td>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{timeAgo(a.timestamp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ display: 'flex', justifyContent: 'center', gap: 8, marginTop: 16 }}>
            {offset > 0 && <button className="btn" onClick={() => navigate('attempts', { offset: offset - limit })}>Previous</button>}
            {items.length >= limit && <button className="btn" onClick={() => navigate('attempts', { offset: offset + limit })}>Next</button>}
          </div>
        </div>
      )}
    </div>
  );
}
