import { useState, useEffect, useCallback } from 'react';
import { getHealth } from './api';
import { formatUptime } from './utils';
import Overview from './views/Overview';
import Sessions from './views/Sessions';
import Timeline from './views/Timeline';
import Execution from './views/Execution';
import Attempts from './views/Attempts';
import Control from './views/Control';

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

export default function App() {
  const [view, setView] = useState({ name: 'overview', params: {} });
  const [theme, setTheme] = useState(() => localStorage.getItem('walacor_theme') || 'dark');
  const [health, setHealth] = useState(null);
  const [time, setTime] = useState(new Date());

  // Theme management
  useEffect(() => {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    localStorage.setItem('walacor_theme', theme);
  }, [theme]);

  // Clock
  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  // Health polling
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
  }, []);

  const toggleTheme = () => {
    document.documentElement.classList.add('theme-transitioning');
    setTheme(t => t === 'dark' ? 'light' : 'dark');
    setTimeout(() => document.documentElement.classList.remove('theme-transitioning'), 350);
  };

  const tabs = ['overview', 'sessions', 'attempts', 'control'];
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
      default: return <Overview navigate={navigate} health={health} />;
    }
  };

  return (
    <>
      {/* Header */}
      <header className="header">
        <div className="brand">
          <span className="brand-diamond">◆</span>
          <span className="brand-text">WALACOR</span>
          <span className="brand-sub">LINEAGE</span>
        </div>
        <div className="header-right">
          <span className="header-time">
            {time.toLocaleTimeString('en-US', { hour12: false })}
          </span>
          <button className="theme-toggle" onClick={toggleTheme} title="Toggle theme">
            {theme === 'dark' ? '☀' : '☾'}
          </button>
          <div className="status-pill">
            <StatusPulse status={status} />
            <span>{status}</span>
          </div>
          {health && (
            <span className="header-time">
              {health.enforcement_mode && health.enforcement_mode}
              {health.uptime_seconds != null && ` · ${formatUptime(health.uptime_seconds)}`}
            </span>
          )}
        </div>
      </header>

      {/* Nav */}
      <nav className="nav">
        <div className="nav-inner">
          {tabs.map(t => (
            <button
              key={t}
              className={`tab${activeTab === t ? ' active' : ''}`}
              onClick={() => navigate(t)}
            >
              {t}
            </button>
          ))}
        </div>
      </nav>

      {/* Main content */}
      <main className="main">
        {renderView()}
      </main>
    </>
  );
}
