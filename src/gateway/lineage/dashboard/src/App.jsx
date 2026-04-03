import { useState, useEffect, useCallback } from 'react';
import { getHealth, getSessions } from './api';
import { formatUptime } from './utils';
import Overview from './views/Overview';
import Sessions from './views/Sessions';
import Timeline from './views/Timeline';
import Execution from './views/Execution';
import Attempts from './views/Attempts';
import Control from './views/Control';
import Compliance from './views/Compliance';
import Playground from './views/Playground';

/* ── Sidebar Icons — minimal stroked geometry, control-panel feel ── */
const navIcons = {
  overview: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="currentColor">
      <rect x="2" y="2" width="5.5" height="5.5" rx="1"/>
      <rect x="10.5" y="2" width="5.5" height="5.5" rx="1"/>
      <rect x="2" y="10.5" width="5.5" height="5.5" rx="1"/>
      <rect x="10.5" y="10.5" width="5.5" height="5.5" rx="1"/>
    </svg>
  ),
  sessions: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M2 5h14M2 9h14M2 13h10"/>
    </svg>
  ),
  attempts: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 9h3l2-5 3 10 2-5h6"/>
    </svg>
  ),
  control: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M2 4h4m4 0h6M2 9h8m4 0h2M2 14h2m4 0h8"/>
      <circle cx="9" cy="4" r="1.5" fill="currentColor"/>
      <circle cx="13" cy="9" r="1.5" fill="currentColor"/>
      <circle cx="5" cy="14" r="1.5" fill="currentColor"/>
    </svg>
  ),
  compliance: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 1.5L2.5 4.5v5c0 4 3 6.5 6.5 7.5 3.5-1 6.5-3.5 6.5-7.5v-5L9 1.5z"/>
      <path d="M6.5 9l2 2 3.5-3.5"/>
    </svg>
  ),
  playground: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 5l5 4-5 4M9 14h7"/>
    </svg>
  ),
};

function readViewFromUrl() {
  if (typeof window === 'undefined') return { name: 'overview', params: {} };
  const sp = new URLSearchParams(window.location.search);
  if (sp.get('view') === 'sessions') {
    return {
      name: 'sessions',
      params: {
        offset: Math.max(0, parseInt(sp.get('offset') || '0', 10) || 0),
        q: sp.get('q') || '',
        sort: ['last_activity', 'record_count', 'model'].includes(sp.get('sort'))
          ? sp.get('sort')
          : 'last_activity',
        order: sp.get('order') === 'asc' ? 'asc' : 'desc',
      },
    };
  }
  if (sp.get('view') === 'attempts') {
    const sortKeys = ['timestamp', 'disposition', 'request_id', 'user', 'model_id', 'path', 'status_code'];
    return {
      name: 'attempts',
      params: {
        offset: Math.max(0, parseInt(sp.get('offset') || '0', 10) || 0),
        q: sp.get('q') || '',
        sort: sortKeys.includes(sp.get('sort')) ? sp.get('sort') : 'timestamp',
        order: sp.get('order') === 'asc' ? 'asc' : 'desc',
      },
    };
  }
  return { name: 'overview', params: {} };
}

function StatusPulse({ status }) {
  const colors = { healthy: '#34d399', degraded: '#f59e0b', fail_closed: '#ef4444' };
  const c = colors[status] || '#4c4c60';
  return (
    <span style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
      <span style={{
        position: 'absolute', width: 14, height: 14, borderRadius: '50%',
        backgroundColor: c, opacity: 0.4, animation: 'ping 2s cubic-bezier(0,0,0.2,1) infinite',
      }} />
      <span style={{ position: 'relative', width: 10, height: 10, borderRadius: '50%', backgroundColor: c }} />
    </span>
  );
}

function HashTicker() {
  const [items, setItems] = useState([]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await getSessions(12, 0);
        const sessions = data.sessions || [];
        const entries = sessions.map(s => ({
          id: s.session_id?.slice(0, 8) || '???',
          full: s.session_id || '',
          model: s.model || 'unknown',
          count: s.record_count || 0,
        }));
        if (!cancelled && entries.length > 0) setItems(entries);
      } catch {}
    };
    load();
    const t = setInterval(load, 30000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  if (items.length === 0) return null;

  // Duplicate for seamless CSS loop
  const doubled = [...items, ...items];

  return (
    <div className="hash-ticker">
      <div className="hash-ticker-track">
        {doubled.map((h, i) => (
          <span key={i} className="hash-ticker-item">
            <span className="ticker-label">SES:{h.id}</span>
            <span>{h.full.replace(/-/g, '')}</span>
            <span style={{ color: 'var(--gold-dim)', fontSize: 9 }}>{h.model} [{h.count}]</span>
            {i < doubled.length - 1 && <span className="hash-ticker-sep">◆</span>}
          </span>
        ))}
      </div>
    </div>
  );
}

export default function App() {
  const [view, setView] = useState(() => readViewFromUrl());
  const [theme, setTheme] = useState(() => localStorage.getItem('walacor_theme') || 'dark');
  const [health, setHealth] = useState(null);
  const [time, setTime] = useState(new Date());
  const [sidebarOpen, setSidebarOpen] = useState(() => localStorage.getItem('walacor_sidebar') !== 'collapsed');

  useEffect(() => {
    localStorage.setItem('walacor_sidebar', sidebarOpen ? 'expanded' : 'collapsed');
  }, [sidebarOpen]);

  useEffect(() => {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    localStorage.setItem('walacor_theme', theme);
  }, [theme]);

  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const poll = async () => {
      try { setHealth(await getHealth()); } catch { setHealth(null); }
    };
    poll();
    const t = setInterval(poll, 30000);
    return () => clearInterval(t);
  }, []);

  const navigate = useCallback((name, params = {}) => {
    setView({ name, params });
    if (typeof window === 'undefined') return;
    const path = window.location.pathname.split('?')[0];
    if (name === 'sessions') {
      const p = { offset: 0, q: '', sort: 'last_activity', order: 'desc', ...params };
      const sp = new URLSearchParams();
      sp.set('view', 'sessions');
      sp.set('offset', String(Math.max(0, Number(p.offset) || 0)));
      const qq = String(p.q || '').trim();
      if (qq) sp.set('q', qq);
      const st = ['last_activity', 'record_count', 'model'].includes(p.sort) ? p.sort : 'last_activity';
      sp.set('sort', st);
      sp.set('order', p.order === 'asc' ? 'asc' : 'desc');
      window.history.replaceState({}, '', `${path}?${sp.toString()}`);
    } else if (name === 'attempts') {
      const p = { offset: 0, q: '', sort: 'timestamp', order: 'desc', ...params };
      const sp = new URLSearchParams();
      sp.set('view', 'attempts');
      sp.set('offset', String(Math.max(0, Number(p.offset) || 0)));
      const qq = String(p.q || '').trim();
      if (qq) sp.set('q', qq);
      const st = ['timestamp', 'disposition', 'request_id', 'user', 'model_id', 'path', 'status_code'].includes(p.sort)
        ? p.sort
        : 'timestamp';
      sp.set('sort', st);
      sp.set('order', p.order === 'asc' ? 'asc' : 'desc');
      window.history.replaceState({}, '', `${path}?${sp.toString()}`);
    } else {
      window.history.replaceState({}, '', path);
    }
  }, []);

  const toggleTheme = () => {
    document.documentElement.classList.add('theme-transitioning');
    setTheme(t => t === 'dark' ? 'light' : 'dark');
    setTimeout(() => document.documentElement.classList.remove('theme-transitioning'), 350);
  };

  const tabs = ['overview', 'sessions', 'attempts', 'control', 'compliance', 'playground'];
  const activeTab = ['overview', 'sessions', 'timeline', 'execution'].includes(view.name)
    ? (view.name === 'timeline' || view.name === 'execution' ? 'sessions' : view.name)
    : view.name === 'attempts' ? 'attempts' : view.name;

  const status = health?.status || 'offline';

  const renderView = () => {
    switch (view.name) {
      case 'overview': return <Overview navigate={navigate} health={health} />;
      case 'sessions': return <Sessions navigate={navigate} params={view.params} />;
      case 'timeline': return <Timeline navigate={navigate} sessionId={view.params.sessionId} />;
      case 'execution': return <Execution navigate={navigate} executionId={view.params.executionId} sessionId={view.params.sessionId} />;
      case 'attempts': return <Attempts navigate={navigate} params={view.params} />;
      case 'control': return <Control navigate={navigate} params={view.params} health={health} />;
      case 'compliance': return <Compliance navigate={navigate} />;
      case 'playground': return <Playground navigate={navigate} />;
      default: return <Overview navigate={navigate} health={health} />;
    }
  };

  return (
    <div className="app-layout">
      {/* ── Sidebar ── */}
      <aside className={`sidebar${sidebarOpen ? ' expanded' : ''}`}>
        <div className="sidebar-brand" onClick={() => navigate('overview')} title="Walacor Lineage">
          <span className="sidebar-diamond">◆</span>
          <div className="sidebar-brand-text">
            <span className="sidebar-brand-name">WALACOR</span>
            <span className="sidebar-brand-sub">LINEAGE</span>
          </div>
        </div>

        <nav className="sidebar-nav">
          {tabs.map(t => (
            <button
              key={t}
              className={`sidebar-item${activeTab === t ? ' active' : ''}`}
              onClick={() => navigate(t)}
              title={t.charAt(0).toUpperCase() + t.slice(1)}
            >
              <span className="sidebar-icon">{navIcons[t]}</span>
              <span className="sidebar-label">{t}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <button className="sidebar-item" onClick={toggleTheme} title="Toggle theme">
            <span className="sidebar-icon" style={{ fontSize: 16 }}>
              {theme === 'dark' ? '☀' : '☾'}
            </span>
            <span className="sidebar-label">{theme === 'dark' ? 'light mode' : 'dark mode'}</span>
          </button>
          <button className="sidebar-toggle" onClick={() => setSidebarOpen(o => !o)} title={sidebarOpen ? 'Collapse' : 'Expand'}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
              style={{ transform: sidebarOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.25s ease' }}>
              <path d="M6 3l5 5-5 5"/>
            </svg>
          </button>
        </div>
      </aside>

      {/* ── Content Area ── */}
      <div className={`content-area${sidebarOpen ? ' shifted' : ''}`}>
        {/* Status Strip */}
        <header className="header">
          <div className="header-left">
            <button className="sidebar-mobile-toggle" onClick={() => setSidebarOpen(o => !o)}>
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M2 4h14M2 9h14M2 14h14"/>
              </svg>
            </button>
            <span className="header-view-label">{view.name.replace(/^./, c => c.toUpperCase())}</span>
          </div>
          <div className="header-right">
            {/* Subsystem status row */}
            <div className="subsystem-row">
              {[
                {
                  label: 'WAL',
                  /* Remote Walacor writes use `storage`; local lineage uses `wal` */
                  ok: !!(health?.wal || health?.storage),
                  hint: health?.storage
                    ? 'Walacor backend connected'
                    : health?.wal
                      ? 'Local WAL active'
                      : 'No audit persistence',
                },
                { label: 'PROVIDERS', ok: health?.status === 'healthy', hint: 'Gateway reachability' },
                { label: 'CHAIN', ok: !!health?.session_chain, hint: 'Session chain tracker' },
                { label: 'BUDGET', ok: health?.status !== 'fail_closed', hint: 'Not in fail-closed mode' },
                {
                  label: 'ANALYZERS',
                  ok: (health?.content_analyzers ?? 0) > 0,
                  hint: `${health?.content_analyzers ?? 0} content analyzer(s)`,
                },
              ].map(s => (
                <div
                  key={s.label}
                  className="subsystem-dot"
                  title={`${s.label}: ${s.ok ? 'OK' : 'attention'} — ${s.hint ?? ''}`}
                >
                  <span className={`subsystem-indicator ${s.ok ? 'ok' : 'down'}`} />
                  <span className="subsystem-label">{s.label}</span>
                </div>
              ))}
            </div>
            <span className="header-sep">│</span>
            <span className="header-time">
              {time.toLocaleTimeString('en-US', { hour12: false })}
            </span>
            <div className="status-pill">
              <StatusPulse status={status} />
              <span>{status}</span>
            </div>
            {health && (
              <span className="header-meta">
                {health.enforcement_mode}
                {health.uptime_seconds != null && <><span className="header-sep">·</span>{formatUptime(health.uptime_seconds)}</>}
              </span>
            )}
          </div>
        </header>

        <main className="main">
          {renderView()}
        </main>
      </div>

      {/* ── Hash Ticker — scrolling SHA3-512 hashes ── */}
      <HashTicker />
    </div>
  );
}
