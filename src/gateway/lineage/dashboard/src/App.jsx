import { useState, useEffect, useCallback, lazy, Suspense } from 'react';
import { getHealth } from './api';
import { formatUptime, isTabVisible } from './utils';
import './styles/app-shell.css';
import Overview from './views/Overview';
const Intelligence = lazy(() => import('./views/Intelligence'));
const Sessions = lazy(() => import('./views/Sessions'));
const Timeline = lazy(() => import('./views/Timeline'));
const Execution = lazy(() => import('./views/Execution'));
const Attempts = lazy(() => import('./views/Attempts'));
const Control = lazy(() => import('./views/Control'));
const Compliance = lazy(() => import('./views/Compliance'));
const Playground = lazy(() => import('./views/Playground'));

const NAV_ITEMS = [
  { key: 'overview', label: 'Overview' },
  { key: 'intelligence', label: 'Governance' },
  { key: 'sessions', label: 'Sessions' },
  { key: 'attempts', label: 'Attempts' },
  { key: 'control', label: 'Control' },
  { key: 'compliance', label: 'Compliance' },
  { key: 'playground', label: 'Playground' },
];

const VIEW_LABELS = {
  overview: 'Overview',
  intelligence: 'Governance',
  sessions: 'Sessions',
  attempts: 'Attempts',
  control: 'Control',
  compliance: 'Compliance',
  playground: 'Playground',
  timeline: 'Sessions',
  execution: 'Sessions',
};

const navIcons = {
  overview: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="currentColor">
      <rect x="2" y="2" width="5.5" height="5.5" rx="1" />
      <rect x="10.5" y="2" width="5.5" height="5.5" rx="1" />
      <rect x="2" y="10.5" width="5.5" height="5.5" rx="1" />
      <rect x="10.5" y="10.5" width="5.5" height="5.5" rx="1" />
    </svg>
  ),
  intelligence: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 1.5l6 3v4.5c0 3-2.5 6-6 7.5-3.5-1.5-6-4.5-6-7.5V4.5l6-3z" />
      <path d="M9 6v3l2 2" />
    </svg>
  ),
  sessions: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M2 5h14M2 9h14M2 13h10" />
    </svg>
  ),
  attempts: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 9h3l2-5 3 10 2-5h6" />
    </svg>
  ),
  control: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <path d="M2 4h4m4 0h6M2 9h8m4 0h2M2 14h2m4 0h8" />
      <circle cx="9" cy="4" r="1.5" fill="currentColor" />
      <circle cx="13" cy="9" r="1.5" fill="currentColor" />
      <circle cx="5" cy="14" r="1.5" fill="currentColor" />
    </svg>
  ),
  compliance: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 1.5L2.5 4.5v5c0 4 3 6.5 6.5 7.5 3.5-1 6.5-3.5 6.5-7.5v-5L9 1.5z" />
      <path d="M6.5 9l2 2 3.5-3.5" />
    </svg>
  ),
  playground: (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 5l5 4-5 4M9 14h7" />
    </svg>
  ),
};

function readViewFromUrl() {
  if (typeof window === 'undefined') return { name: 'overview', params: {} };
  const sp = new URLSearchParams(window.location.search);
  const v = sp.get('view');
  if (v === 'sessions') {
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
  if (v === 'attempts') {
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
  if (v === 'intelligence') return { name: 'intelligence', params: {} };
  if (v === 'control') return { name: 'control', params: {} };
  if (v === 'compliance') return { name: 'compliance', params: {} };
  if (v === 'playground') return { name: 'playground', params: {} };
  return { name: 'overview', params: {} };
}

export default function App() {
  const [view, setView] = useState(() => readViewFromUrl());
  const [theme, setTheme] = useState(() => localStorage.getItem('walacor_theme') || 'dark');
  const [health, setHealth] = useState(null);
  const [time, setTime] = useState(() => new Date());
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

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
      try {
        setHealth(await getHealth());
      } catch {
        setHealth(null);
      }
    };
    poll();
    const id = setInterval(() => {
      if (isTabVisible()) poll();
    }, 30000);
    return () => clearInterval(id);
  }, []);

  const navigate = useCallback((name, params = {}) => {
    setView({ name, params });
    setMobileNavOpen(false);
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
      return;
    }
    if (name === 'attempts') {
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
      return;
    }
    if (name === 'intelligence') {
      const sp = new URLSearchParams();
      sp.set('view', 'intelligence');
      window.history.replaceState({}, '', `${path}?${sp.toString()}`);
      return;
    }
    if (name === 'control') {
      const sp = new URLSearchParams();
      sp.set('view', 'control');
      window.history.replaceState({}, '', `${path}?${sp.toString()}`);
      return;
    }
    if (name === 'compliance') {
      const sp = new URLSearchParams();
      sp.set('view', 'compliance');
      window.history.replaceState({}, '', `${path}?${sp.toString()}`);
      return;
    }
    if (name === 'playground') {
      const sp = new URLSearchParams();
      sp.set('view', 'playground');
      window.history.replaceState({}, '', `${path}?${sp.toString()}`);
      return;
    }
    if (name === 'overview' || name === 'timeline' || name === 'execution') {
      window.history.replaceState({}, '', path);
    }
  }, []);

  const toggleTheme = () => {
    document.documentElement.classList.add('theme-transitioning');
    setTheme((t) => (t === 'dark' ? 'light' : 'dark'));
    setTimeout(() => document.documentElement.classList.remove('theme-transitioning'), 350);
  };

  const activeNavKey = ['overview', 'sessions', 'timeline', 'execution'].includes(view.name)
    ? view.name === 'timeline' || view.name === 'execution'
      ? 'sessions'
      : view.name
    : view.name;

  const viewTitle = VIEW_LABELS[view.name] || 'Overview';
  const utcTime = time.toISOString().slice(11, 19);

  const subsystems = [
    {
      label: 'WAL',
      ok: !!(health?.wal || health?.storage),
    },
    { label: 'PROVIDERS', ok: health?.status === 'healthy' },
    { label: 'CHAIN', ok: !!health?.session_chain },
    { label: 'BUDGET', ok: health?.status !== 'fail_closed' },
    {
      label: 'ANALYZERS',
      ok: (health?.content_analyzers ?? 0) > 0,
    },
  ];

  const viewFallback = (
    <div className="view-loading" style={{ padding: '40px 20px', opacity: 0.5, fontSize: 13 }}>
      Loading…
    </div>
  );

  const renderView = () => {
    switch (view.name) {
      case 'overview':
        return <Overview navigate={navigate} health={health} />;
      case 'intelligence':
        return (
          <Suspense fallback={viewFallback}>
            <Intelligence navigate={navigate} />
          </Suspense>
        );
      case 'sessions':
        return (
          <Suspense fallback={viewFallback}>
            <Sessions navigate={navigate} params={view.params} />
          </Suspense>
        );
      case 'timeline':
        return (
          <Suspense fallback={viewFallback}>
            <Timeline navigate={navigate} sessionId={view.params.sessionId} />
          </Suspense>
        );
      case 'execution':
        return (
          <Suspense fallback={viewFallback}>
            <Execution
              navigate={navigate}
              executionId={view.params.executionId}
              sessionId={view.params.sessionId}
            />
          </Suspense>
        );
      case 'attempts':
        return (
          <Suspense fallback={viewFallback}>
            <Attempts navigate={navigate} params={view.params} />
          </Suspense>
        );
      case 'control':
        return (
          <Suspense fallback={viewFallback}>
            <Control navigate={navigate} params={view.params} health={health} />
          </Suspense>
        );
      case 'compliance':
        return (
          <Suspense fallback={viewFallback}>
            <Compliance />
          </Suspense>
        );
      case 'playground':
        return (
          <Suspense fallback={viewFallback}>
            <Playground />
          </Suspense>
        );
      default:
        return <Overview navigate={navigate} health={health} />;
    }
  };

  return (
    <div className={`tz-shell app${mobileNavOpen ? ' tz-mobile-nav-open' : ''}`}>
      <button
        type="button"
        className="tz-backdrop"
        aria-label="Close menu"
        onClick={() => setMobileNavOpen(false)}
      />
      <aside className="sidebar">
        <button type="button" className="sb-brand" title="TruzenAI" onClick={() => navigate('overview')}>
          <span className="sb-diamond">◆</span>
        </button>
        <nav className="sb-nav">
          {NAV_ITEMS.map((it) => (
            <button
              key={it.key}
              type="button"
              className={`sb-item${activeNavKey === it.key ? ' active' : ''}`}
              title={it.label.toUpperCase()}
              onClick={() => navigate(it.key)}
            >
              {navIcons[it.key]}
            </button>
          ))}
        </nav>
      </aside>

      <div className="content">
        <header className="topbar">
          <div className="topbar-left">
            <button
              type="button"
              className="tz-mobile-menu-btn"
              aria-label="Open navigation"
              onClick={() => setMobileNavOpen((o) => !o)}
            >
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M2 4h14M2 9h14M2 14h14" />
              </svg>
            </button>
            <span className="tz-view-label">{viewTitle}</span>
          </div>
          <div className="topbar-right">
            <div className="topbar-sub">
              {subsystems.map((s) => (
                <span key={s.label} className="topbar-sub-dot" title={s.label}>
                  <span className={`topbar-sub-indicator ${s.ok ? 'ok' : 'down'}`} />
                  <span>{s.label}</span>
                </span>
              ))}
            </div>
            <span className="sep">│</span>
            <span className="time">{utcTime} UTC</span>
            <span className="sep">│</span>
            <span className="tz-meta-compact">
              {health?.enforcement_mode ?? '—'}
              {health?.uptime_seconds != null && (
                <>
                  <span className="sep"> · </span>
                  {formatUptime(health.uptime_seconds)}
                </>
              )}
            </span>
            <span className="sep">│</span>
            <button
              type="button"
              className="theme-toggle"
              onClick={toggleTheme}
              title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
              aria-label="Toggle theme"
            >
              <span className="theme-toggle-track">
                <span className={`theme-toggle-thumb ${theme}`}>
                  {theme === 'dark' ? (
                    <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
                      <path fill="currentColor" d="M6 0a6 6 0 1 0 6 6A4 4 0 0 1 6 0z" />
                    </svg>
                  ) : (
                    <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
                      <circle cx="8" cy="8" r="3.2" fill="currentColor" />
                      <g stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
                        <line x1="8" y1="0.8" x2="8" y2="2.6" />
                        <line x1="8" y1="13.4" x2="8" y2="15.2" />
                        <line x1="0.8" y1="8" x2="2.6" y2="8" />
                        <line x1="13.4" y1="8" x2="15.2" y2="8" />
                        <line x1="2.9" y1="2.9" x2="4.2" y2="4.2" />
                        <line x1="11.8" y1="11.8" x2="13.1" y2="13.1" />
                        <line x1="2.9" y1="13.1" x2="4.2" y2="11.8" />
                        <line x1="11.8" y1="4.2" x2="13.1" y2="2.9" />
                      </g>
                    </svg>
                  )}
                </span>
              </span>
              <span className="theme-toggle-label">{theme === 'dark' ? 'DARK' : 'LIGHT'}</span>
            </button>
          </div>
        </header>

        <main className="tz-main fade-in">{renderView()}</main>
      </div>
    </div>
  );
}
