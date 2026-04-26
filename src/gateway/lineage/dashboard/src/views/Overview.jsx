import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { getSessions, getAttempts, getThroughputHistory, getTokenLatency } from '../api';
import { timeAgo, formatNumber, formatUptime, displayModel, formatSessionId, isTabVisible } from '../utils';
import { ThroughputChart, TokenChart, LatencyChart } from '../components/SvgCharts';
import '../styles/overview-v2.css';

const POLL_MS = 3000;
const RANGE_SECONDS = { '1h': 3600, '24h': 86400, '7d': 604800, '30d': 2592000 };
const RANGES = [
  { key: '1h',  label: '1H' },
  { key: '24h', label: '24H' },
  { key: '7d',  label: '7D' },
  { key: '30d', label: '30D' },
];

function useLineageTheme() {
  const [isLight, setIsLight] = useState(
    () => typeof document !== 'undefined' && document.documentElement.getAttribute('data-theme') === 'light',
  );
  useEffect(() => {
    const el = document.documentElement;
    const sync = () => setIsLight(el.getAttribute('data-theme') === 'light');
    sync();
    const mo = new MutationObserver(sync);
    mo.observe(el, { attributes: true, attributeFilter: ['data-theme'] });
    return () => mo.disconnect();
  }, []);
  return isLight;
}

function dispositionMeta(d) {
  if (!d) return { cls: 'disp-allowed', label: '-' };
  if (d === 'allowed' || d === 'forwarded') return { cls: 'disp-allowed', label: 'ALLOW' };
  if (d.startsWith('denied')) return { cls: 'disp-blocked', label: 'BLOCK' };
  if (d.startsWith('error'))  return { cls: 'disp-error',   label: 'ERROR' };
  return { cls: 'disp-allowed', label: d.toUpperCase() };
}

// ─── Status Strip (7 cells — exact design order) ─────────────────────────────
function StatusStrip({ health, sessions, total, pctAllowed }) {
  const ok = health?.status === 'healthy';
  return (
    <div className="status-strip">
      <div className="status-inner">

        <div className="status-cell health">
          <div className="health-row">
            <span className="health-dot-wrap">
              <span className="health-dot-ping" style={!ok ? { background: 'var(--amber)' } : {}} />
              <span className="health-dot" style={!ok ? { background: 'var(--amber)', boxShadow: '0 0 10px var(--amber)' } : {}} />
            </span>
            <div>
              <div className="health-label" style={!ok ? { color: 'var(--amber)' } : {}}>
                {ok ? 'ALL CLEAR' : (health?.status || 'OFFLINE').toUpperCase()}
              </div>
              <div className="health-sub">gateway · {health?.status || 'offline'}</div>
            </div>
          </div>
        </div>

        <div className="status-cell">
          <div className="status-cell-label">Sessions</div>
          <div className="status-cell-value">{sessions}</div>
        </div>

        <div className="status-cell">
          <div className="status-cell-label">Total Requests</div>
          <div className="status-cell-value" title="Counted from gateway_attempts (1 row per inbound request). Token totals below are per-execution and may exceed this when a single request spawns tool calls or retries.">{total == null ? '—' : formatNumber(total)}</div>
        </div>

        <div className="status-cell value-green">
          <div className="status-cell-label">% Allowed</div>
          <div className="status-cell-value">{pctAllowed == null ? '—' : `${pctAllowed}%`}</div>
        </div>

        <div className="status-cell mode">
          <div className="status-cell-label">Enforcement</div>
          <div className="status-cell-value">
            <span className="status-mode-badge">{health?.enforcement_mode || 'unknown'}</span>
          </div>
        </div>

        <div className="status-cell value-blue">
          <div className="status-cell-label">Analyzers</div>
          <div className="status-cell-value">{health?.content_analyzers ?? '--'}</div>
        </div>

        <div className="status-cell">
          <div className="status-cell-label">Uptime</div>
          <div className="status-cell-value">
            {health?.uptime_seconds != null ? formatUptime(health.uptime_seconds) : '--'}
          </div>
        </div>

      </div>
    </div>
  );
}

// ─── Range Bar (4 options, matches design) ────────────────────────────────────
function RangeBar({ range, setRange }) {
  return (
    <div className="ov2-range-bar">
      <div className="range-left">
        {range === '1h' && <span className="range-pulse-dot" />}
        <span className="range-title">Time range</span>
        <span className="range-sub">· throughput · tokens · latency</span>
      </div>
      <div className="range-buttons">
        {RANGES.map(o => (
          <button key={o.key}
                  className={`range-btn-v2${range === o.key ? ' active' : ''}`}
                  onClick={() => setRange(o.key)}>
            {o.label}
            {range === o.key && o.key === '1h' && <span className="live-dot" />}
          </button>
        ))}
      </div>
    </div>
  );
}

// ─── Counters (with delta arrows + tick animation) ────────────────────────────
function Counters({ counters, prev, variant = 'flush' }) {
  const items = [
    { key: 'rps',   label: 'req/s',    value: counters.rps < 0.1 && counters.rps > 0 ? counters.rps.toFixed(2) : counters.rps.toFixed(1), unit: '', color: 'gold',  dot: 'var(--gold)' },
    { key: 'tps',   label: 'tokens/s', value: counters.tps < 1 ? counters.tps.toFixed(1) : formatNumber(Math.round(counters.tps)),        unit: '', color: '',      dot: 'var(--blue)' },
    { key: 'pct',   label: 'allowed',  value: counters.pct.toFixed(1),                                                                    unit: '%', color: 'green', dot: 'var(--green)' },
    { key: 'total', label: 'total',    value: formatNumber(counters.total),                                                               unit: '', color: '',      dot: 'var(--text-muted)' },
  ];
  const gridClass = variant === 'spotlight' ? 'counters variant-spotlight' : 'counters';
  return (
    <div className={gridClass}>
      {items.map(c => {
        const cur = counters[c.key] || 0;
        const pv  = prev ? (prev[c.key] || 0) : null;
        const delta = pv != null ? cur - pv : 0;
        const changed = pv != null && Math.abs(delta) > 0.001;
        return (
          <div key={c.key} className={['counter', c.color].filter(Boolean).join(' ')}>
            <div className="counter-label">
              <span className="counter-dot" style={{ background: c.dot }} />
              {c.label}
            </div>
            <div className={`counter-value ${c.color} ${changed ? 'counter-tick' : ''}`} key={c.value}>
              {c.value}
              {c.unit && <span className="counter-unit">{c.unit}</span>}
            </div>
            <div className="counter-delta">
              {delta > 0.001  ? <span className="up">↑ {Math.abs(delta).toFixed(c.key === 'total' ? 0 : 2)}</span> :
               delta < -0.001 ? <span className="down">↓ {Math.abs(delta).toFixed(c.key === 'total' ? 0 : 2)}</span> :
                                <span>—</span>} <span style={{ opacity: 0.5 }}>vs prev</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── Loading skeleton ─────────────────────────────────────────────────────────
function Skeleton() {
  return (
    <div>
      <div className="skeleton-block" style={{ height: 62, marginBottom: 14 }} />
      <div className="skeleton-block" style={{ height: 38, marginBottom: 14 }} />
      <div className="skeleton-block" style={{ height: 340, marginBottom: 14 }} />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 14 }}>
        <div className="skeleton-block" style={{ height: 240 }} />
        <div className="skeleton-block" style={{ height: 240 }} />
      </div>
    </div>
  );
}

// ─── Main Overview ────────────────────────────────────────────────────────────
export default function Overview({ navigate, health }) {
  const isLight = useLineageTheme();
  /** Default spotlight (handoff); set localStorage wal_counters_variant=flush for the strip layout. */
  const counterVariant = useMemo(() => {
    try {
      const v = localStorage.getItem('wal_counters_variant');
      if (v === 'flush') return 'flush';
      return 'spotlight';
    } catch {
      return 'spotlight';
    }
  }, []);

  const [sessions,   setSessions]   = useState([]);
  const [attempts,   setAttempts]   = useState([]);
  const [attStats,   setAttStats]   = useState({});
  const [attTotal,   setAttTotal]   = useState(0);
  const [loading,    setLoading]    = useState(true);
  const [error,      setError]      = useState(null);
  const [newSessionIds,  setNewSessionIds]  = useState(() => new Set());
  const [newActivityIds, setNewActivityIds] = useState(() => new Set());

  const [range, setRange] = useState('1h');
  const [tpData, setTpData] = useState([]);
  const [tkData, setTkData] = useState([]);
  const [ltData, setLtData] = useState([]);
  const [tpLoading, setTpLoading] = useState(false);
  const [counters, setCounters] = useState({ rps: 0, tps: 0, pct: 100, total: 0 });
  const [prevCounters, setPrevCounters] = useState(null);
  const [tokenSnap, setTokenSnap] = useState({ prompt: 0, completion: 0 });
  const [latencySnap, setLatencySnap] = useState({ avg: 0 });
  const [hoverIdx, setHoverIdx] = useState(null);

  // Track previous counter snapshot every ~5 polls for delta display
  const tickRef = useRef(0);
  useEffect(() => {
    tickRef.current++;
    if (tickRef.current % 5 === 0) {
      setPrevCounters({ ...counters });
    }
  }, [counters.total]); // eslint-disable-line react-hooks/exhaustive-deps

  // Sessions + activity — detect new entries for row-enter animation
  const prevSessionIdsRef = useRef(new Set());
  const prevActivityIdsRef = useRef(new Set());

  useEffect(() => {
    let cancelled = false;
    const refresh = async (isFirst) => {
      try {
        const [sessData, attData] = await Promise.all([getSessions(6, 0), getAttempts(8, 0)]);
        if (cancelled) return;
        const ss = sessData.sessions || [];
        const aa = attData.attempts || attData.items || [];

        if (!isFirst) {
          const prevSess = prevSessionIdsRef.current;
          const freshSess = new Set(ss.map(s => s.session_id).filter(id => !prevSess.has(id)));
          if (freshSess.size > 0) {
            setNewSessionIds(freshSess);
            setTimeout(() => setNewSessionIds(new Set()), 800);
          }
          const prevAct = prevActivityIdsRef.current;
          const freshAct = new Set(aa.map((a, i) => a.execution_id || `r${i}`).filter(id => !prevAct.has(id)));
          if (freshAct.size > 0) {
            setNewActivityIds(freshAct);
            setTimeout(() => setNewActivityIds(new Set()), 800);
          }
        }
        prevSessionIdsRef.current  = new Set(ss.map(s => s.session_id));
        prevActivityIdsRef.current = new Set(aa.map((a, i) => a.execution_id || `r${i}`));

        setSessions(ss);
        setAttempts(aa);
        setAttStats(attData.stats || {});
        setAttTotal(attData.total || 0);
        setError(null);
      } catch (e) {
        if (!cancelled && isFirst) setError(e.message);
      } finally {
        if (!cancelled && isFirst) setLoading(false);
      }
    };
    refresh(true);
    const id = setInterval(() => { if (isTabVisible()) refresh(false); }, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  // Charts
  const applySummary = useCallback((tpRows, tkRows, rng) => {
    // tpRows[i].requests is a per-bucket COUNT (despite the legacy `rps`
    // chart-key still being plotted as the bar height). Sum the counts and
    // divide by the full range duration to get the average rps over the
    // window — not a per-bucket rate.
    const total   = tpRows.reduce((s, d) => s + (d.requests || d.rps || 0), 0);
    const allowed = tpRows.reduce((s, d) => s + (d.allowed || 0), 0);
    const secs    = RANGE_SECONDS[rng] || 3600;
    const rps     = secs > 0 ? total / secs : 0;
    const promptSum = tkRows.reduce((s, d) => s + (d.prompt || 0), 0);
    const compSum   = tkRows.reduce((s, d) => s + (d.completion || 0), 0);
    const tps = secs > 0 ? (promptSum + compSum) / secs : 0;
    const pct = total > 0 ? (allowed / total * 100) : 100;
    setCounters({ rps, tps, pct, total });
    setTokenSnap({ prompt: promptSum, completion: compSum });
    let wSum = 0, wCount = 0;
    for (const d of tkRows) {
      const c = d.count || 0;
      if (c > 0) { wSum += (d.avg || 0) * c; wCount += c; }
    }
    setLatencySnap({ avg: wCount > 0 ? wSum / wCount : 0 });
  }, []);

  useEffect(() => {
    const needsDate = range === '7d' || range === '30d';
    const label = (t) => t ? (needsDate ? t.substring(5, 16).replace('T', ' ') : t.substring(11, 16)) : '';
    const mapThroughput = (d) => (d.buckets || []).map(b => {
      const requests = b.request_count ?? b.total ?? 0;
      return {
        t: label(b.t),
        // `requests` is the per-bucket count (correctly named). `rps` is
        // kept as an alias because the chart still references the legacy
        // dataKey; treat it as the bar height, not a rate.
        requests,
        rps: requests,
        allowed: b.allowed || 0,
        blocked: b.blocked != null ? b.blocked : Math.max(0, requests - (b.allowed || 0)),
      };
    });
    const mapTkLt = (d) => (d.buckets || []).map(b => ({
      t:          label(b.t),
      prompt:     b.prompt_tokens    ?? 0,
      completion: b.completion_tokens ?? 0,
      avg:        b.avg_latency_ms   ?? 0,
      count:      b.request_count    ?? 0,
    }));

    let cancelled = false;
    const load = async (isFirst) => {
      if (isFirst) { setTpLoading(true); setTpData([]); setTkData([]); setLtData([]); }
      try {
        const [td, tld] = await Promise.all([getThroughputHistory(range), getTokenLatency(range)]);
        if (cancelled) return;
        const tpRows = mapThroughput(td);
        const tkRows = mapTkLt(tld);
        setTpData(tpRows);
        setTkData(tkRows);
        setLtData(tkRows.map(({ t, avg }) => ({ t, avg })));
        applySummary(tpRows, tkRows, range);
      } catch { /* retain prior on refresh error */ }
      if (!cancelled && isFirst) setTpLoading(false);
    };

    load(true);
    if (range === '1h') {
      const id = setInterval(() => {
        if (!cancelled && isTabVisible()) load(false);
      }, POLL_MS);
      return () => { cancelled = true; clearInterval(id); };
    }
    return () => { cancelled = true; };
  }, [range, applySummary]);

  // Memoized so the palette object reference is stable across renders while
  // isLight is unchanged. Prevents spurious style-prop updates on the legend
  // swatches (which would otherwise receive a new `{background: ...}` object
  // every render, defeating React's style-attribute fast path).
  const P = useMemo(
    () => isLight
      ? { gold: '#9a6700', green: '#15803d', red: '#dc2626', blue: '#2563eb' }
      : { gold: '#c9a84c', green: '#34d399', red: '#ef4444', blue: '#60a5fa' },
    [isLight],
  );

  if (loading) return <Skeleton />;
  if (error)   return <div className="error-card">Error: {error}</div>;

  // Anchor both "total" and "% allowed" to the same source (the selected time
  // window from the throughput buckets) so the header never disagrees with the
  // chart underneath. Until the first throughput fetch lands we show a dash
  // rather than briefly blending unbounded attempt totals with the windowed %.
  const haveWindow = tpData.length > 0;
  const totalRequests = haveWindow ? Math.round(counters.total) : null;
  const pctAllowedDisplay = haveWindow ? counters.pct.toFixed(1) : null;

  return (
    <div className="fade-child">

      <StatusStrip
        health={health}
        sessions={sessions.length}
        total={totalRequests}
        pctAllowed={pctAllowedDisplay}
      />

      <RangeBar range={range} setRange={setRange} />

      {/* ── Throughput ── */}
      <div className="card card-accent-top throughput-card" style={{ position: 'relative' }}>
        <div className="card-header">
          <span className="card-title">◇ Throughput</span>
          <div className="chart-legend">
            <span className="chart-legend-item"><span className="chart-legend-swatch" style={{ background: P.gold }} />req/s</span>
            <span className="chart-legend-item"><span className="chart-legend-swatch" style={{ background: P.green }} />allowed</span>
            <span className="chart-legend-item"><span className="chart-legend-swatch" style={{ background: P.red }} />blocked</span>
          </div>
        </div>

        {tpLoading ? (
          <div className="skeleton-block throughput-chart-wrap" style={{ margin: 0 }} />
        ) : tpData.length > 0 ? (
          <div className="throughput-chart-wrap">
            <ThroughputChart data={tpData} hoverIdx={hoverIdx} setHoverIdx={setHoverIdx} isLight={isLight} />
          </div>
        ) : (
          <div className="throughput-chart-wrap" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)' }}>no data for this range</span>
          </div>
        )}

        <Counters counters={counters} prev={prevCounters} variant={counterVariant} />
      </div>

      {/* ── Token + Latency twin ── */}
      <div className="twin-grid">
        <div className="card twin-card" style={{ position: 'relative' }}>
          <div className="card-header">
            <span className="card-title">◇ Token Usage</span>
            <div className="twin-summary">
              <div className="twin-stat">
                <span className="twin-stat-label">Prompt</span>
                <span className="twin-stat-value blue">{formatNumber(tokenSnap.prompt)}</span>
              </div>
              <div className="twin-stat">
                <span className="twin-stat-label">Completion</span>
                <span className="twin-stat-value gold">{formatNumber(tokenSnap.completion)}</span>
              </div>
              <div className="twin-stat">
                <span className="twin-stat-label">Total</span>
                <span className="twin-stat-value">{formatNumber(tokenSnap.prompt + tokenSnap.completion)}</span>
              </div>
            </div>
          </div>
          {tkData.length > 0 ? (
            <TokenChart data={tkData} isLight={isLight} />
          ) : (
            <div className="chart-wrap" style={{ height: 170, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)' }}>
                {tpLoading ? 'loading…' : 'no token data'}
              </span>
            </div>
          )}
        </div>

        <div className="card twin-card" style={{ position: 'relative' }}>
          <div className="card-header">
            <span className="card-title">◇ Latency</span>
            <div className="twin-summary">
              <div className="twin-stat">
                <span className="twin-stat-label">Average</span>
                <span className="twin-stat-value gold">
                  {Math.round(latencySnap.avg)}
                  <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 4 }}>ms</span>
                </span>
              </div>
              <div className="twin-stat">
                <span className="twin-stat-label">P95 est.</span>
                <span className="twin-stat-value">
                  {Math.round(latencySnap.avg * 1.8)}
                  <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 4 }}>ms</span>
                </span>
              </div>
            </div>
          </div>
          {ltData.length > 0 ? (
            <LatencyChart data={ltData} isLight={isLight} />
          ) : (
            <div className="chart-wrap" style={{ height: 170, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)' }}>
                {tpLoading ? 'loading…' : 'no latency data'}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* ── Recent Sessions + Activity ── */}
      <div className="bottom-grid">

        <div className="card" style={{ marginBottom: 0 }}>
          <div className="feed-header">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span className="card-title">◇ Recent Sessions</span>
              <span className="feed-live"><span className="feed-live-dot" />LIVE</span>
            </div>
            <button className="view-all-btn" onClick={() => navigate('sessions')}>View all →</button>
          </div>
          {sessions.length === 0 ? (
            <div className="ov2-empty">No sessions yet</div>
          ) : sessions.map(s => (
            <div
              key={s.session_id}
              className={`session-row${newSessionIds.has(s.session_id) ? ' new' : ''}`}
              onClick={() => navigate('timeline', { sessionId: s.session_id })}
            >
              <div className="session-id-col">
                <span className="session-id-text">{formatSessionId(s.session_id)}</span>
                <span className="session-meta-text">{displayModel(s.model) || 'unknown'}</span>
              </div>
              <div className="session-count-col">
                <span className="session-count-num">{s.record_count}</span>
                <span className="session-count-lbl">records</span>
              </div>
              <div className="session-time-text">{timeAgo(s.last_activity)}</div>
            </div>
          ))}
        </div>

        <div className="card" style={{ marginBottom: 0 }}>
          <div className="feed-header">
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span className="card-title">◇ Recent Activity</span>
              <span className="feed-live"><span className="feed-live-dot" />LIVE</span>
            </div>
            <button className="view-all-btn" onClick={() => navigate('attempts')}>View all →</button>
          </div>
          {attempts.length === 0 ? (
            <div className="ov2-empty">No activity yet</div>
          ) : attempts.map((a, i) => {
            const rowId = a.execution_id || `r${i}`;
            const meta = dispositionMeta(a.disposition);
            return (
              <div
                key={rowId}
                className={`activity-row${newActivityIds.has(rowId) ? ' new' : ''}`}
                onClick={() => a.execution_id && navigate('execution', { executionId: a.execution_id })}
                style={{ cursor: a.execution_id ? 'pointer' : 'default' }}
              >
                <span className={`disposition-badge ${meta.cls}`}>{meta.label}</span>
                <span className="activity-model">{displayModel(a.model_id)}</span>
                <span className="activity-path"><span className="method">{a.method || 'POST'}</span>{a.path}</span>
                <span className="activity-time">{timeAgo(a.timestamp)}</span>
              </div>
            );
          })}
        </div>

      </div>
    </div>
  );
}
