/* Walacor Gateway — Overview v2
   Main dashboard component */

const { useState, useEffect, useRef, useCallback } = React;

const POLL_MS = 3000;

const navIcons = {
  overview: <svg width="18" height="18" viewBox="0 0 18 18" fill="currentColor"><rect x="2" y="2" width="5.5" height="5.5"/><rect x="10.5" y="2" width="5.5" height="5.5"/><rect x="2" y="10.5" width="5.5" height="5.5"/><rect x="10.5" y="10.5" width="5.5" height="5.5"/></svg>,
  intel: <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M9 1.5l6 3v4.5c0 3-2.5 6-6 7.5-3.5-1.5-6-4.5-6-7.5V4.5l6-3z"/><path d="M9 6v3l2 2"/></svg>,
  sessions: <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M2 5h14M2 9h14M2 13h10"/></svg>,
  attempts: <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M1 9h3l2-5 3 10 2-5h6"/></svg>,
  control: <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M2 4h4m4 0h6M2 9h8m4 0h2M2 14h2m4 0h8"/><circle cx="9" cy="4" r="1.5" fill="currentColor"/><circle cx="13" cy="9" r="1.5" fill="currentColor"/><circle cx="5" cy="14" r="1.5" fill="currentColor"/></svg>,
  compliance: <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M9 1.5L2.5 4.5v5c0 4 3 6.5 6.5 7.5 3.5-1 6.5-3.5 6.5-7.5v-5L9 1.5z"/><path d="M6.5 9l2 2 3.5-3.5"/></svg>,
  playground: <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2 5l5 4-5 4M9 14h7"/></svg>,
};

const NAV_ITEMS = [
  { key: 'overview', label: 'Overview' },
  { key: 'intel', label: 'Intelligence' },
  { key: 'sessions', label: 'Sessions' },
  { key: 'attempts', label: 'Attempts' },
  { key: 'control', label: 'Control' },
  { key: 'compliance', label: 'Compliance' },
  { key: 'playground', label: 'Playground' },
];

// ── Sidebar ────────────────────────────────────────────────────────────────
function Sidebar({ activeView, setActiveView }) {
  return (
    <aside className="sidebar">
      <div className="sb-brand"><span className="sb-diamond">◆</span></div>
      <nav className="sb-nav">
        {NAV_ITEMS.map(it => (
          <button
            key={it.key}
            className={`sb-item${it.key === activeView ? ' active' : ''}`}
            title={it.label.toUpperCase()}
            onClick={() => setActiveView(it.key)}>
            {navIcons[it.key]}
          </button>
        ))}
      </nav>
    </aside>
  );
}

// ── Topbar ─────────────────────────────────────────────────────────────────
function Topbar({ health, theme, setTheme, viewLabel }) {
  const [time, setTime] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  const subsystems = [
    { label: 'WAL', ok: true },
    { label: 'PROVIDERS', ok: true },
    { label: 'CHAIN', ok: !!health.session_chain },
    { label: 'BUDGET', ok: true },
    { label: 'ANALYZERS', ok: (health.content_analyzers ?? 0) > 0 },
  ];
  return (
    <header className="topbar">
      <div className="topbar-left">{viewLabel}</div>
      <div className="topbar-right">
        <div className="topbar-sub">
          {subsystems.map(s => (
            <span key={s.label} className="topbar-sub-dot" title={s.label}>
              <span className={`topbar-sub-indicator ${s.ok ? 'ok' : 'down'}`} />
              <span>{s.label}</span>
            </span>
          ))}
        </div>
        <span className="sep">│</span>
        <span className="time">{time.toTimeString().slice(0, 8)} UTC</span>
        <span className="sep">│</span>
        <span>{health.enforcement_mode} · {formatUptime(health.uptime_seconds)}</span>
        <span className="sep">│</span>
        <button
          className="theme-toggle"
          onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
          title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
          aria-label="Toggle theme">
          <span className="theme-toggle-track">
            <span className={`theme-toggle-thumb ${theme}`}>
              {theme === 'dark' ? (
                <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true"><path fill="currentColor" d="M6 0a6 6 0 1 0 6 6A4 4 0 0 1 6 0z"/></svg>
              ) : (
                <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true"><circle cx="8" cy="8" r="3.2" fill="currentColor"/><g stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><line x1="8" y1="0.8" x2="8" y2="2.6"/><line x1="8" y1="13.4" x2="8" y2="15.2"/><line x1="0.8" y1="8" x2="2.6" y2="8"/><line x1="13.4" y1="8" x2="15.2" y2="8"/><line x1="2.9" y1="2.9" x2="4.2" y2="4.2"/><line x1="11.8" y1="11.8" x2="13.1" y2="13.1"/><line x1="2.9" y1="13.1" x2="4.2" y2="11.8"/><line x1="11.8" y1="4.2" x2="13.1" y2="2.9"/></g></svg>
              )}
            </span>
          </span>
          <span className="theme-toggle-label">{theme === 'dark' ? 'DARK' : 'LIGHT'}</span>
        </button>
      </div>
    </header>
  );
}

// ── Status Strip ───────────────────────────────────────────────────────────
function StatusStrip({ health, sessions, total, pctAllowed }) {
  return (
    <div className="status-strip">
      <div className="status-inner">
        <div className="status-cell health">
          <div className="health-row">
            <span className="health-dot-wrap">
              <span className="health-dot-ping" />
              <span className="health-dot" />
            </span>
            <div>
              <div className="health-label">ALL CLEAR</div>
              <div className="health-sub">gateway · {health.status}</div>
            </div>
          </div>
        </div>
        <div className="status-cell">
          <div className="status-cell-label">Sessions</div>
          <div className="status-cell-value">{sessions}</div>
        </div>
        <div className="status-cell">
          <div className="status-cell-label">Total Requests</div>
          <div className="status-cell-value">{formatNumber(total)}</div>
        </div>
        <div className="status-cell value-green">
          <div className="status-cell-label">% Allowed</div>
          <div className="status-cell-value">{pctAllowed}%</div>
        </div>
        <div className="status-cell mode">
          <div className="status-cell-label">Enforcement</div>
          <div className="status-cell-value">
            <span className="status-mode-badge">{health.enforcement_mode}</span>
          </div>
        </div>
        <div className="status-cell value-blue">
          <div className="status-cell-label">Analyzers</div>
          <div className="status-cell-value">{health.content_analyzers}</div>
        </div>
        <div className="status-cell">
          <div className="status-cell-label">Uptime</div>
          <div className="status-cell-value">{formatUptime(health.uptime_seconds)}</div>
        </div>
      </div>
    </div>
  );
}

// ── Range Selector ─────────────────────────────────────────────────────────
function RangeBar({ range, setRange }) {
  const opts = [
    { key: '1h', label: '1H' },
    { key: '24h', label: '24H' },
    { key: '7d', label: '7D' },
    { key: '30d', label: '30D' },
  ];
  return (
    <div className="range-bar">
      <div className="range-left">
        {range === '1h' && <span className="range-pulse-dot" />}
        <span className="range-title">Time range</span>
        <span className="range-sub">· throughput · tokens · latency</span>
      </div>
      <div className="range-buttons">
        {opts.map(o => (
          <button key={o.key} className={`range-btn${range === o.key ? ' active' : ''}`}
                  onClick={() => setRange(o.key)}>
            {o.label}
            {range === o.key && o.key === '1h' && <span className="live-dot" />}
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Counters ───────────────────────────────────────────────────────────────
function Counters({ counters, prev, variant }) {
  const items = [
    { label: 'req/s', value: counters.rps.toFixed(2), unit: '', color: 'gold', dot: '#c9a84c', key: 'rps' },
    { label: 'tokens/s', value: Math.round(counters.tps).toString(), unit: '', color: '', dot: '#60a5fa', key: 'tps' },
    { label: 'allowed', value: counters.pct.toFixed(1), unit: '%', color: 'green', dot: '#34d399', key: 'pct' },
    { label: 'total', value: formatNumber(counters.total), unit: '', color: '', dot: '#65657c', key: 'total' },
  ];
  return (
    <div className={`counters ${variant === 'spotlight' ? 'variant-spotlight' : ''}`}>
      {items.map((c, i) => {
        const changed = prev && Math.abs((prev[c.key] || 0) - counters[c.key]) > 0.001;
        const delta = prev ? counters[c.key] - (prev[c.key] || 0) : 0;
        return (
          <div key={c.key} className={`counter ${c.color}`}>
            <div className="counter-label">
              <span className="counter-dot" style={{ background: c.dot }} />
              {c.label}
            </div>
            <div className={`counter-value ${c.color} ${changed ? 'counter-tick' : ''}`} key={c.value}>
              {c.value}
              {c.unit && <span className="counter-unit">{c.unit}</span>}
            </div>
            <div className="counter-delta">
              {delta > 0.001 ? <span className="up">↑ {Math.abs(delta).toFixed(c.key === 'total' ? 0 : 2)}</span> :
               delta < -0.001 ? <span className="down">↓ {Math.abs(delta).toFixed(c.key === 'total' ? 0 : 2)}</span> :
               <span>—</span>} <span style={{ opacity: 0.5 }}>vs prev</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Sessions List ──────────────────────────────────────────────────────────
function SessionsList({ sessions, onClick, newIds }) {
  if (!sessions.length) return <div className="empty">No sessions yet</div>;
  return (
    <>
      {sessions.map(s => (
        <div key={s.session_id}
             className={`session-row ${newIds.has(s.session_id) ? 'new' : ''}`}
             onClick={() => onClick(s.session_id)}>
          <div className="session-id-col">
            <span className="session-id">{formatSessionId(s.session_id)}</span>
            <span className="session-meta">{s.model}</span>
          </div>
          <div>
            <span className="session-records">{s.record_count}</span>
            <span className="session-records-label">records</span>
          </div>
          <div className="session-time">{timeAgo(s.last_activity)}</div>
        </div>
      ))}
    </>
  );
}

// ── Activity Feed ──────────────────────────────────────────────────────────
function ActivityFeed({ activity, onClick, newIds }) {
  if (!activity.length) return <div className="empty">No activity yet</div>;
  return (
    <>
      {activity.map(a => {
        const meta = dispositionMeta(a.disposition);
        return (
          <div key={a.execution_id}
               className={`activity-row ${newIds.has(a.execution_id) ? 'new' : ''}`}
               onClick={() => onClick(a.execution_id)}>
            <span className={`disposition-badge ${meta.cls}`}>{meta.label}</span>
            <span className="activity-model">{a.model_id}</span>
            <span className="activity-path"><span className="method">{a.method}</span>{a.path}</span>
            <span className="activity-time">{timeAgo(a.timestamp)}</span>
          </div>
        );
      })}
    </>
  );
}

// ── Tweaks Panel ───────────────────────────────────────────────────────────
function TweaksPanel({ open, onClose, tweaks, setTweaks }) {
  return (
    <div className={`tweaks-panel ${open ? 'open' : ''}`}>
      <div className="tweaks-head">
        <span className="tweaks-title">◆ Tweaks</span>
        <button className="tweaks-close" onClick={onClose}>✕</button>
      </div>
      <div className="tweak-row">
        <label className="tweak-label">Counters style</label>
        <div className="tweak-btn-group">
          {['flush', 'spotlight'].map(v => (
            <button key={v}
                    className={`tweak-btn${tweaks.counters === v ? ' active' : ''}`}
                    onClick={() => setTweaks({ ...tweaks, counters: v })}>
              {v}
            </button>
          ))}
        </div>
      </div>
      <div className="tweak-row">
        <label className="tweak-label">Live data stream</label>
        <div className="tweak-btn-group">
          {['on', 'paused'].map(v => (
            <button key={v}
                    className={`tweak-btn${tweaks.live === v ? ' active' : ''}`}
                    onClick={() => setTweaks({ ...tweaks, live: v })}>
              {v}
            </button>
          ))}
        </div>
      </div>
      <div className="tweak-row">
        <label className="tweak-label">Gateway health</label>
        <div className="tweak-btn-group">
          {['healthy', 'degraded'].map(v => (
            <button key={v}
                    className={`tweak-btn${tweaks.health === v ? ' active' : ''}`}
                    onClick={() => setTweaks({ ...tweaks, health: v })}>
              {v}
            </button>
          ))}
        </div>
      </div>
      <div className="tweak-row">
        <label className="tweak-label">Bottom split</label>
        <div className="tweak-btn-group">
          {['balanced', 'activity-wide'].map(v => (
            <button key={v}
                    className={`tweak-btn${tweaks.split === v ? ' active' : ''}`}
                    onClick={() => setTweaks({ ...tweaks, split: v })}>
              {v}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Main App ───────────────────────────────────────────────────────────────
function Overview() {
  const [range, setRange] = useState('1h');
  const [tpData, setTpData] = useState(() => MockData.genThroughput('1h'));
  const [tkData, setTkData] = useState(() => MockData.genTokens('1h'));
  const [sessions, setSessions] = useState(() => MockData.genSessions());
  const [activity, setActivity] = useState(() => MockData.genActivity());
  const [hoverIdx, setHoverIdx] = useState(null);
  const [prevCounters, setPrevCounters] = useState(null);
  const [navToast, setNavToast] = useState(null);
  const [newSessionIds, setNewSessionIds] = useState(new Set());
  const [newActivityIds, setNewActivityIds] = useState(new Set());

  const [activeView, setActiveView] = useState(() => {
    try { return localStorage.getItem('wal-view') || 'overview'; } catch (e) { return 'overview'; }
  });
  useEffect(() => {
    try { localStorage.setItem('wal-view', activeView); } catch (e) {}
  }, [activeView]);

  const [tweaks, setTweaks] = useState({
    counters: 'flush',
    live: 'on',
    health: 'healthy',
    split: 'balanced',
  });
  const [tweaksOpen, setTweaksOpen] = useState(false);

  const [theme, setThemeState] = useState(() => {
    try { return localStorage.getItem('wal-theme') || 'light'; } catch (e) { return 'light'; }
  });
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('wal-theme', theme); } catch (e) {}
  }, [theme]);
  const setTheme = (t) => setThemeState(t);

  // Load data whenever range changes
  useEffect(() => {
    setTpData(MockData.genThroughput(range));
    setTkData(MockData.genTokens(range));
  }, [range]);

  // Poll for 1H live refresh
  useEffect(() => {
    if (range !== '1h' || tweaks.live !== 'on') return;
    const id = setInterval(() => {
      setTpData(prev => {
        const next = [...prev];
        // Drop first, add new at end
        const last = next[next.length - 1];
        const rps = Math.max(0.05, last.rps + (Math.random() - 0.5) * 0.8);
        const total = rps * 60;
        const blockRate = 0.04 + Math.random() * 0.06;
        const blocked = Math.round(total * blockRate);
        const allowed = Math.round(total - blocked);
        next.shift();
        next.push({ i: last.i + 1, t: new Date().toTimeString().slice(0, 5), rps, allowed, blocked, total: allowed + blocked });
        return next;
      });
      setTkData(prev => {
        const next = [...prev];
        const last = next[next.length - 1];
        const prompt = Math.max(200, last.prompt + Math.round((Math.random() - 0.5) * 800));
        const completion = Math.max(80, last.completion + Math.round((Math.random() - 0.5) * 400));
        const avg = Math.max(80, last.avg + Math.round((Math.random() - 0.5) * 60));
        next.shift();
        next.push({ i: last.i + 1, t: new Date().toTimeString().slice(0, 5), prompt, completion, avg, count: 20 + Math.round(Math.random() * 60) });
        return next;
      });

      // Occasionally prepend new activity + session
      if (Math.random() > 0.5) {
        const newAct = MockData.genActivity().slice(0, 1)[0];
        newAct.timestamp = new Date().toISOString();
        setActivity(prev => [newAct, ...prev.slice(0, 9)]);
        setNewActivityIds(new Set([newAct.execution_id]));
        setTimeout(() => setNewActivityIds(new Set()), 800);
      }
      if (Math.random() > 0.85) {
        const newSess = MockData.genSessions().slice(0, 1)[0];
        newSess.last_activity = new Date().toISOString();
        setSessions(prev => [newSess, ...prev.slice(0, 5)]);
        setNewSessionIds(new Set([newSess.session_id]));
        setTimeout(() => setNewSessionIds(new Set()), 800);
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [range, tweaks.live]);

  // Derived
  const total = tpData.reduce((s, d) => s + d.total, 0);
  const allowed = tpData.reduce((s, d) => s + d.allowed, 0);
  const pct = total > 0 ? (allowed / total) * 100 : 100;
  const secs = { '1h': 3600, '24h': 86400, '7d': 604800, '30d': 2592000 }[range];
  const rps = secs > 0 ? total / secs : 0;
  const promptSum = tkData.reduce((s, d) => s + d.prompt, 0);
  const compSum = tkData.reduce((s, d) => s + d.completion, 0);
  const tkTotal = promptSum + compSum;
  const tps = secs > 0 ? tkTotal / secs : 0;
  let wSum = 0, wCount = 0;
  for (const d of tkData) { if (d.count > 0) { wSum += d.avg * d.count; wCount += d.count; } }
  const avgLatency = wCount > 0 ? wSum / wCount : 0;

  const counters = { rps, tps, pct, total };

  // Track previous counters for delta (update every few polls)
  const tickRef = useRef(0);
  useEffect(() => {
    tickRef.current++;
    if (tickRef.current % 5 === 0) {
      setPrevCounters({ rps, tps, pct, total });
    }
  }, [total]);

  const health = {
    ...MockData.health,
    status: tweaks.health,
  };

  const navigate = useCallback((target, label) => {
    setNavToast(`→ ${label}`);
    setTimeout(() => setNavToast(null), 1800);
  }, []);

  // Tweaks host integration
  useEffect(() => {
    const handler = (e) => {
      if (!e.data || typeof e.data !== 'object') return;
      if (e.data.type === '__activate_edit_mode') setTweaksOpen(true);
      if (e.data.type === '__deactivate_edit_mode') setTweaksOpen(false);
    };
    window.addEventListener('message', handler);
    window.parent.postMessage({ type: '__edit_mode_available' }, '*');
    return () => window.removeEventListener('message', handler);
  }, []);

  const bottomGridStyle = tweaks.split === 'activity-wide'
    ? { gridTemplateColumns: '1fr 2fr' }
    : { gridTemplateColumns: '1fr 1.4fr' };

  return (
    <div className="app">
      <Sidebar activeView={activeView} setActiveView={setActiveView} />
      <div className="content">
        <Topbar health={health} theme={theme} setTheme={setTheme} viewLabel={NAV_ITEMS.find(i => i.key === activeView)?.label || 'Overview'} />
        <main className="main fade-in" key={activeView}>
        {activeView === 'intel' && <IntelligenceView showToast={(t) => { setNavToast(t); setTimeout(() => setNavToast(null), 2200); }} />}
        {activeView === 'sessions' && <SessionsView />}
        {activeView === 'attempts' && <AttemptsView />}
        {activeView === 'control' && <ControlStub />}
        {activeView === 'compliance' && <ComplianceStub />}
        {activeView === 'playground' && <PlaygroundStub />}
        {activeView === 'overview' && (<React.Fragment>
          <StatusStrip
            health={health}
            sessions={sessions.length}
            total={total}
            pctAllowed={pct.toFixed(1)}
          />

          <RangeBar range={range} setRange={setRange} />

          <div className="card card-accent throughput-card">
            <div className="card-header">
              <span className="card-title">◇ Throughput</span>
              <div className="legend">
                <span className="legend-item"><span className="legend-swatch" style={{ background: 'var(--gold)' }} />req/s</span>
                <span className="legend-item"><span className="legend-swatch" style={{ background: 'var(--green)' }} />allowed</span>
                <span className="legend-item"><span className="legend-swatch" style={{ background: 'var(--red)' }} />blocked</span>
              </div>
            </div>
            <ThroughputChart data={tpData} hoverIdx={hoverIdx} setHoverIdx={setHoverIdx} theme={theme} />
            <Counters counters={counters} prev={prevCounters} variant={tweaks.counters} />
          </div>

          <div className="twin-grid">
            <div className="card twin-card">
              <div className="card-header">
                <span className="card-title">◇ Token Usage</span>
                <div className="twin-summary">
                  <div className="twin-stat">
                    <span className="twin-stat-label">Prompt</span>
                    <span className="twin-stat-value blue">{formatNumber(promptSum)}</span>
                  </div>
                  <div className="twin-stat">
                    <span className="twin-stat-label">Completion</span>
                    <span className="twin-stat-value gold">{formatNumber(compSum)}</span>
                  </div>
                  <div className="twin-stat">
                    <span className="twin-stat-label">Total</span>
                    <span className="twin-stat-value">{formatNumber(tkTotal)}</span>
                  </div>
                </div>
              </div>
              <TokenChart data={tkData} theme={theme} />
            </div>
            <div className="card twin-card">
              <div className="card-header">
                <span className="card-title">◇ Latency</span>
                <div className="twin-summary">
                  <div className="twin-stat">
                    <span className="twin-stat-label">Average</span>
                    <span className="twin-stat-value gold">{Math.round(avgLatency)}<span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 4 }}>ms</span></span>
                  </div>
                  <div className="twin-stat">
                    <span className="twin-stat-label">P95 est.</span>
                    <span className="twin-stat-value">{Math.round(avgLatency * 1.8)}<span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 4 }}>ms</span></span>
                  </div>
                </div>
              </div>
              <LatencyChart data={tkData} theme={theme} />
            </div>
          </div>

          <div className="bottom-grid" style={bottomGridStyle}>
            <div className="card">
              <div className="feed-header">
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span className="card-title">◇ Recent Sessions</span>
                  <span className="feed-live"><span className="feed-live-dot" />LIVE</span>
                </div>
                <button className="view-all" onClick={() => navigate('sessions', 'Sessions')}>View all →</button>
              </div>
              <SessionsList
                sessions={sessions}
                newIds={newSessionIds}
                onClick={(id) => navigate('timeline', `Timeline · ${id.substring(0, 8)}`)}
              />
            </div>
            <div className="card">
              <div className="feed-header">
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span className="card-title">◇ Recent Activity</span>
                  <span className="feed-live"><span className="feed-live-dot" />LIVE</span>
                </div>
                <button className="view-all" onClick={() => navigate('attempts', 'Attempts')}>View all →</button>
              </div>
              <ActivityFeed
                activity={activity}
                newIds={newActivityIds}
                onClick={(id) => navigate('execution', `Execution · ${id.substring(0, 8)}`)}
              />
            </div>
          </div>
        </React.Fragment>)}
        </main>
      </div>

      {/* Nav toast */}
      {navToast && (
        <div style={{
          position: 'fixed',
          bottom: 24, left: '50%',
          transform: 'translateX(-50%)',
          background: 'var(--bg-elevated)',
          border: '1px solid var(--gold-dim)',
          padding: '10px 18px',
          fontFamily: 'var(--mono)',
          fontSize: 12,
          color: 'var(--gold)',
          letterSpacing: '0.1em',
          boxShadow: '0 8px 24px var(--shadow)',
          zIndex: 300,
          animation: 'fadeIn 0.2s ease',
        }}>
          {navToast}
        </div>
      )}

      <TweaksPanel
        open={tweaksOpen}
        onClose={() => setTweaksOpen(false)}
        tweaks={tweaks}
        setTweaks={setTweaks}
      />
    </div>
  );
}

// Mount
ReactDOM.createRoot(document.getElementById('root')).render(<Overview />);
