import { useState, useEffect, useRef, useCallback } from 'react';
import { AreaChart, Area, BarChart, Bar, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { getHealth, getSessions, getAttempts, getMetrics, getTokenLatency, getThroughputHistory, parsePrometheusMetrics, sumMetric } from '../api';
import { timeAgo, formatNumber, formatUptime, displayModel, formatSessionId, dispositionClass, dispositionLabel, getTokenCount, policyBadgeClass } from '../utils';

// Fixed 60-second live window: 21 slots × 3s polling = 63s visible
const LIVE_SLOTS = 21;
const LIVE_TICKS = [0, 5, 10, 15, 20];
function liveTickLabel(v) {
  const s = (20 - v) * 3;
  return s === 0 ? 'now' : `-${s}s`;
}
// Position data from right: newest at index 20, oldest slides left
function assignLivePos(arr) {
  return arr.map((d, i) => ({ ...d, t: LIVE_SLOTS - 1 - (arr.length - 1 - i) }));
}

function RangeSelector({ active, onChange }) {
  const opts = [
    { key: 'current', label: 'Live' },
    { key: '1h', label: '1H' },
    { key: '24h', label: '24H' },
    { key: '7d', label: '7D' },
    { key: '30d', label: '30D' },
  ];
  return (
    <div className="range-bar">
      {opts.map(o => (
        <button key={o.key} className={`range-btn${active === o.key ? ' active' : ''}`} onClick={() => onChange(o.key)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function GovernanceBadge({ label, value, ok }) {
  return (
    <div style={{ padding: '14px 16px', background: 'var(--bg-inset)', border: '1px solid var(--border)', borderRadius: 0 }}>
      <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: 6 }}>{label}</div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 500, color: ok ? 'var(--green)' : 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 6 }}>
        {ok && <span style={{ fontSize: 11 }}>✓</span>}
        {value}
      </div>
    </div>
  );
}

export default function Overview({ navigate, health }) {
  const [sessions, setSessions] = useState([]);
  const [attempts, setAttempts] = useState([]);
  const [attStats, setAttStats] = useState({});
  const [attTotal, setAttTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Live chart data
  const [tpRange, setTpRange] = useState('current');
  const [tlRange, setTlRange] = useState('current');
  const [tpData, setTpData] = useState([]);
  const [tkData, setTkData] = useState([]);
  const [ltData, setLtData] = useState([]);
  const [counters, setCounters] = useState({ rps: 0, tps: 0, pct: 100, total: 0 });
  const prevMetrics = useRef(null);
  const prevTime = useRef(null);
  const prevTokens = useRef(null);
  const prevLatency = useRef(null);
  const latestTokenSnap = useRef({ prompt: 0, completion: 0 });
  const latestLatencySnap = useRef({ avg: 0 });

  // Initial data load
  useEffect(() => {
    (async () => {
      try {
        const [sessData, attData] = await Promise.all([
          getSessions(6, 0),
          getAttempts(8, 0),
        ]);
        setSessions(sessData.sessions || []);
        setAttempts(attData.items || []);
        setAttStats(attData.stats || {});
        setAttTotal(attData.total || 0);
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // Live throughput polling
  useEffect(() => {
    if (tpRange !== 'current') {
      // Historical
      (async () => {
        try {
          const data = await getThroughputHistory(tpRange);
          const buckets = data.buckets || [];
          setTpData(buckets.map(b => ({
            t: b.t?.substring(11, 16) || '',
            rps: b.request_count || 0,
            allowed: b.allowed || 0,
            blocked: (b.request_count || 0) - (b.allowed || 0),
          })));
        } catch {}
      })();
      return;
    }

    prevMetrics.current = null;
    prevTime.current = null;
    setTpData([]);

    const poll = async () => {
      try {
        const text = await getMetrics();
        const m = parsePrometheusMetrics(text);
        const now = Date.now();
        const totalReqs = sumMetric(m, 'walacor_gateway_requests_total', '');
        const allowedReqs = sumMetric(m, 'walacor_gateway_requests_total', 'outcome="allowed"');
        const blockedReqs = totalReqs - allowedReqs;
        const totalTokens = sumMetric(m, 'walacor_gateway_token_usage_total', '');

        if (prevMetrics.current && prevTime.current) {
          const dt = (now - prevTime.current) / 1000;
          if (dt > 0) {
            const rps = Math.max(0, (totalReqs - prevMetrics.current.totalReqs) / dt);
            const allowed = Math.max(0, (allowedReqs - prevMetrics.current.allowedReqs) / dt);
            const blocked = Math.max(0, (blockedReqs - prevMetrics.current.blockedReqs) / dt);
            const tps = Math.max(0, (totalTokens - prevMetrics.current.totalTokens) / dt);
            const pct = totalReqs > 0 ? (allowedReqs / totalReqs * 100) : 100;

            setTpData(prev => {
              const next = [...prev, { rps, allowed, blocked }].slice(-LIVE_SLOTS);
              return assignLivePos(next);
            });
            setCounters({ rps, tps, pct, total: totalReqs });
          }
        }
        prevMetrics.current = { totalReqs, allowedReqs, blockedReqs, totalTokens };
        prevTime.current = now;
      } catch {}
    };

    poll();
    const t = setInterval(poll, 3000);
    return () => clearInterval(t);
  }, [tpRange]);

  // Token + Latency polling
  useEffect(() => {
    if (tlRange !== 'current') {
      (async () => {
        try {
          const data = await getTokenLatency(tlRange);
          const buckets = data.buckets || [];
          setTkData(buckets.map(b => ({
            t: b.t?.substring(11, 16) || '',
            prompt: b.prompt_tokens || 0,
            completion: b.completion_tokens || 0,
          })));
          setLtData(buckets.map(b => ({
            t: b.t?.substring(11, 16) || '',
            avg: b.avg_latency_ms || 0,
            max: b.max_latency_ms || 0,
          })));
        } catch {}
      })();
      return;
    }

    prevTokens.current = null;
    prevLatency.current = null;
    setTkData([]);
    setLtData([]);

    const poll = async () => {
      try {
        const text = await getMetrics();
        const m = parsePrometheusMetrics(text);
        const now = Date.now();
        const promptTok = sumMetric(m, 'walacor_gateway_token_usage_total', 'token_type="prompt"');
        const compTok = sumMetric(m, 'walacor_gateway_token_usage_total', 'token_type="completion"');
        const latencySum = sumMetric(m, 'walacor_gateway_pipeline_duration_seconds_sum', '');
        const latencyCount = sumMetric(m, 'walacor_gateway_pipeline_duration_seconds_count', '');

        latestTokenSnap.current = { prompt: promptTok, completion: compTok };

        if (prevTokens.current) {
          const deltaPrompt = Math.max(0, promptTok - prevTokens.current.prompt);
          const deltaComp = Math.max(0, compTok - prevTokens.current.completion);
          setTkData(prev => {
            const next = [...prev, { prompt: deltaPrompt, completion: deltaComp }].slice(-LIVE_SLOTS);
            return assignLivePos(next);
          });
        }
        prevTokens.current = { prompt: promptTok, completion: compTok };

        if (prevLatency.current) {
          const dSum = latencySum - prevLatency.current.sum;
          const dCount = latencyCount - prevLatency.current.count;
          if (dCount > 0) {
            const intervalAvgMs = (dSum / dCount) * 1000;
            latestLatencySnap.current = { avg: intervalAvgMs };
            setLtData(prev => {
              const next = [...prev, { avg: intervalAvgMs, max: intervalAvgMs * 1.3 }].slice(-LIVE_SLOTS);
              return assignLivePos(next);
            });
          }
        }
        prevLatency.current = { sum: latencySum, count: latencyCount };
      } catch {}
    };

    poll();
    const t = setInterval(poll, 3000);
    return () => clearInterval(t);
  }, [tlRange]);

  if (loading) {
    return (
      <div>
        <div style={{ display: 'flex', gap: 12, marginBottom: 16 }}>
          {[1,2,3,4].map(i => <div key={i} className="skeleton-block" style={{ flex: 1, height: 92 }} />)}
        </div>
        <div className="skeleton-block" style={{ height: 240, marginBottom: 16 }} />
      </div>
    );
  }
  if (error) return <div className="error-card">Error: {error}</div>;

  const allowed = attStats.allowed || 0;
  const denied = Object.keys(attStats).filter(k => k.startsWith('denied')).reduce((s, k) => s + attStats[k], 0);
  const pctAllowed = attTotal > 0 ? (allowed / attTotal * 100).toFixed(1) : '100';

  return (
    <div className="fade-child">
      {/* ── Hero Status ── */}
      <div className="overview-hero" style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: 16, marginBottom: 20, minWidth: 0 }}>
        {/* Primary status */}
        <div className="card card-accent-green" style={{ padding: '28px 24px' }}>
          <div className="card-title" style={{ marginBottom: 16 }}>System Status</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
            <span style={{
              position: 'relative', display: 'inline-flex', alignItems: 'center',
            }}>
              <span style={{
                position: 'absolute', width: 14, height: 14, borderRadius: '50%',
                backgroundColor: health?.status === 'healthy' ? '#34d399' : '#f59e0b',
                opacity: 0.4, animation: 'ping 2s cubic-bezier(0,0,0.2,1) infinite',
              }} />
              <span style={{
                position: 'relative', width: 10, height: 10, borderRadius: '50%',
                backgroundColor: health?.status === 'healthy' ? '#34d399' : '#f59e0b',
              }} />
            </span>
            <span style={{
              fontFamily: 'var(--mono)', fontSize: 26, fontWeight: 600,
              color: health?.status === 'healthy' ? 'var(--green)' : 'var(--amber)',
              letterSpacing: '-0.5px',
            }}>
              {health?.status === 'healthy' ? 'ALL CLEAR' : (health?.status || 'OFFLINE').toUpperCase()}
            </span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
            {[
              [health?.enforcement_mode || '-', 'mode'],
              [health?.uptime_seconds != null ? formatUptime(health.uptime_seconds) : '-', 'uptime'],
              [health?.content_analyzers ?? '0', 'analyzers'],
              [health?.session_chain ? 'enabled' : 'disabled', 'chain'],
            ].map(([val, label], i) => (
              <div key={i} style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                <span style={{ fontFamily: 'var(--mono)', color: 'var(--text-secondary)' }}>{val}</span>
                <span style={{ margin: '0 4px', opacity: 0.3 }}>·</span>{label}
              </div>
            ))}
          </div>
        </div>

        {/* Stat cards */}
        <div className="overview-stats" style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, minWidth: 0 }}>
          {[
            { value: sessions.length, label: 'ACTIVE SESSIONS', sub: '', color: 'var(--text-muted)' },
            { value: formatNumber(attTotal), label: 'TOTAL REQUESTS', sub: `${pctAllowed}% allowed`, color: 'var(--green)' },
            { value: health?.enforcement_mode || '-', label: 'ENFORCEMENT', sub: health?.storage?.backend ? `${health.storage.backend} storage` : '', color: 'var(--text-muted)', isText: true },
          ].map((s, i) => (
            <div key={i} className="stat-card">
              <div className={`stat-value${s.isText ? ' stat-value-text' : ''}`}>{s.value}</div>
              <div className="stat-label">{s.label}</div>
              {s.sub && <div className="stat-sub" style={{ color: s.color }}>{s.sub}</div>}
            </div>
          ))}
        </div>
      </div>

      {/* ── Governance Status ── */}
      <div className="card card-accent-green" style={{ marginBottom: 16 }}>
        <div className="card-title" style={{ marginBottom: 14 }}>Governance Status</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 10 }}>
          <GovernanceBadge label="Enforcement" value={health?.enforcement_mode === 'enforced' ? 'Enforced' : (health?.enforcement_mode || '-')} ok={health?.enforcement_mode === 'enforced'} />
          <GovernanceBadge label="Policy Cache" value={health?.status === 'healthy' || health?.status === 'degraded' ? 'Active' : 'Stale'} ok={health?.status === 'healthy'} />
          <GovernanceBadge label="Content Analysis" value={health?.content_analyzers ? `${health.content_analyzers} analyzer${health.content_analyzers !== 1 ? 's' : ''}` : 'None'} ok={!!health?.content_analyzers} />
          <GovernanceBadge label="Session Chain" value={health?.session_chain ? 'Enabled' : 'Disabled'} ok={!!health?.session_chain} />
        </div>
      </div>

      {/* ── Throughput Chart ── */}
      <div className="card card-accent" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {tpRange === 'current' && <div style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--gold)', boxShadow: '0 0 8px rgba(201,168,76,0.5)', animation: 'pulse 1.5s ease-in-out infinite' }} />}
            <span className="card-title" style={{ marginBottom: 0 }}>Throughput</span>
          </div>
          <RangeSelector active={tpRange} onChange={setTpRange} />
        </div>
        <div style={{ height: 220 }}>
          {tpData.length > 1 ? (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={tpData} margin={{ top: 10, right: 8, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="gradAllowed" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#34d399" stopOpacity={0.25} />
                    <stop offset="100%" stopColor="#34d399" stopOpacity={0.02} />
                  </linearGradient>
                  <linearGradient id="gradBlocked" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#ef4444" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#ef4444" stopOpacity={0.02} />
                  </linearGradient>
                  <linearGradient id="gradGold" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#c9a84c" stopOpacity={0.15} />
                    <stop offset="100%" stopColor="#c9a84c" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" vertical={false} />
                {tpRange === 'current' ? (
                  <XAxis dataKey="t" type="number" domain={[0, 20]} ticks={LIVE_TICKS} tickFormatter={liveTickLabel} tick={{ fontSize: 9, fill: 'var(--chart-label)', fontFamily: 'var(--mono)' }} />
                ) : (
                  <XAxis dataKey="t" tick={{ fontSize: 9, fill: 'var(--chart-label)', fontFamily: 'var(--mono)' }} interval="preserveStartEnd" />
                )}
                <YAxis tick={{ fontSize: 10, fill: 'var(--chart-label)', fontFamily: 'var(--mono)' }} domain={[0, 'auto']} allowDataOverflow={false} />
                <Tooltip contentStyle={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 6, fontSize: 12, fontFamily: 'var(--mono)' }} />
                <Area type="natural" dataKey="blocked" stackId="1" fill="url(#gradBlocked)" stroke="rgba(239,68,68,0.6)" strokeWidth={1} />
                <Area type="natural" dataKey="allowed" stackId="1" fill="url(#gradAllowed)" stroke="rgba(52,211,153,0.6)" strokeWidth={1} />
                <Area type="natural" dataKey="rps" fill="url(#gradGold)" stroke="#c9a84c" strokeWidth={2.5} />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 48 }}>
              {[
                { value: formatNumber(counters.total) || '0', label: 'requests' },
                { value: counters.pct ? counters.pct.toFixed(0) + '%' : '100%', label: 'allowed' },
                { value: counters.rps ? counters.rps.toFixed(1) : '0.0', label: 'req/s' },
              ].map((c, i) => (
                <div key={i} style={{ textAlign: 'center' }}>
                  <div style={{ fontFamily: 'var(--mono)', fontSize: 42, fontWeight: 700, color: 'var(--text-secondary)', letterSpacing: '-2px', lineHeight: 1 }}>{c.value}</div>
                  <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '1px', marginTop: 8 }}>{c.label}</div>
                </div>
              ))}
            </div>
          )}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--border)' }}>
          {[
            { value: counters.rps < 0.1 && counters.rps > 0 ? counters.rps.toFixed(2) : counters.rps.toFixed(1), label: 'req/s', color: 'var(--gold)' },
            { value: counters.tps < 1 ? counters.tps.toFixed(1) : Math.round(counters.tps), label: 'tokens/s', color: 'var(--text-primary)' },
            { value: counters.pct.toFixed(0) + '%', label: 'allowed', color: 'var(--green)' },
            { value: formatNumber(counters.total), label: 'total', color: 'var(--text-primary)' },
          ].map((c, i) => (
            <div key={i} style={{ textAlign: 'center' }}>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 20, fontWeight: 600, color: c.color }}>{c.value}</div>
              <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px', marginTop: 2 }}>{c.label}</div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Token + Latency side by side ── */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ marginBottom: 12 }}>
          <RangeSelector active={tlRange} onChange={setTlRange} />
        </div>
        <div className="charts-side" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          {/* Token Usage */}
          <div className="card" style={{ marginBottom: 0 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
              <span className="card-title" style={{ marginBottom: 0 }}>Token Usage</span>
              <div style={{ display: 'flex', gap: 12, fontSize: 10, color: 'var(--text-muted)' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <span style={{ width: 8, height: 8, background: 'var(--blue)', borderRadius: 1, display: 'inline-block', opacity: 0.7 }} /> Prompt
                </span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <span style={{ width: 8, height: 8, background: 'var(--gold)', borderRadius: 1, display: 'inline-block', opacity: 0.8 }} /> Completion
                </span>
              </div>
            </div>
            <div style={{ height: 150 }}>
              {tkData.length > 1 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={tkData} margin={{ top: 10, right: 8, bottom: 0, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" vertical={false} />
                    {tlRange === 'current' ? (
                      <XAxis dataKey="t" type="number" domain={[0, 20]} ticks={LIVE_TICKS} tickFormatter={liveTickLabel} tick={{ fontSize: 9, fill: 'var(--chart-label)', fontFamily: 'var(--mono)' }} />
                    ) : (
                      <XAxis dataKey="t" tick={{ fontSize: 9, fill: 'var(--chart-label)', fontFamily: 'var(--mono)' }} interval="preserveStartEnd" />
                    )}
                    <YAxis tick={{ fontSize: 9, fill: 'var(--chart-label)', fontFamily: 'var(--mono)' }} domain={[0, 'auto']} allowDataOverflow={false} />
                    <Tooltip contentStyle={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 6, fontSize: 11, fontFamily: 'var(--mono)' }} />
                    <Bar dataKey="prompt" stackId="tokens" fill="var(--blue)" opacity={0.7} />
                    <Bar dataKey="completion" stackId="tokens" fill="var(--gold)" opacity={0.8} radius={[2, 2, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 32 }}>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontFamily: 'var(--mono)', fontSize: 28, fontWeight: 700, color: 'var(--blue)', lineHeight: 1 }}>{formatNumber(latestTokenSnap.current.prompt)}</div>
                    <div style={{ fontSize: 9, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '1px', marginTop: 4 }}>prompt</div>
                  </div>
                  <div style={{ width: 1, height: 32, background: 'var(--border)' }} />
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontFamily: 'var(--mono)', fontSize: 28, fontWeight: 700, color: 'var(--gold)', lineHeight: 1 }}>{formatNumber(latestTokenSnap.current.completion)}</div>
                    <div style={{ fontSize: 9, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '1px', marginTop: 4 }}>completion</div>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Latency */}
          <div className="card" style={{ marginBottom: 0 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
              <span className="card-title" style={{ marginBottom: 0 }}>Latency</span>
              <div style={{ display: 'flex', gap: 12, fontSize: 10, color: 'var(--text-muted)' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <span style={{ width: 10, height: 2, background: 'var(--gold)', borderRadius: 1, display: 'inline-block' }} /> Avg
                </span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  <span style={{ width: 10, height: 8, background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.15)', borderRadius: 1, display: 'inline-block' }} /> P95 band
                </span>
              </div>
            </div>
            <div style={{ height: 150 }}>
              {ltData.length > 1 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={ltData} margin={{ top: 10, right: 8, bottom: 0, left: 0 }}>
                    <defs>
                      <linearGradient id="gradLatency" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#c9a84c" stopOpacity={0.15} />
                        <stop offset="100%" stopColor="#c9a84c" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" vertical={false} />
                    {tlRange === 'current' ? (
                      <XAxis dataKey="t" type="number" domain={[0, 20]} ticks={LIVE_TICKS} tickFormatter={liveTickLabel} tick={{ fontSize: 9, fill: 'var(--chart-label)', fontFamily: 'var(--mono)' }} />
                    ) : (
                      <XAxis dataKey="t" tick={{ fontSize: 9, fill: 'var(--chart-label)', fontFamily: 'var(--mono)' }} interval="preserveStartEnd" />
                    )}
                    <YAxis tick={{ fontSize: 9, fill: 'var(--chart-label)', fontFamily: 'var(--mono)' }} unit="ms" domain={[0, 'auto']} allowDataOverflow={false} />
                    <Tooltip contentStyle={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 6, fontSize: 11, fontFamily: 'var(--mono)' }} formatter={v => [`${Math.round(v)}ms`]} />
                    <Area type="monotone" dataKey="max" fill="rgba(239,68,68,0.08)" stroke="rgba(239,68,68,0.2)" strokeWidth={1} strokeDasharray="4 4" />
                    <Area type="monotone" dataKey="avg" fill="url(#gradLatency)" stroke="#c9a84c" strokeWidth={2} />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontFamily: 'var(--mono)', fontSize: 28, fontWeight: 700, color: 'var(--gold)', lineHeight: 1 }}>{latestLatencySnap.current.avg > 0 ? Math.round(latestLatencySnap.current.avg) + 'ms' : '--'}</div>
                    <div style={{ fontSize: 9, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '1px', marginTop: 4 }}>avg latency</div>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* ── Sessions + Activity side by side ── */}
      <div className="bottom-grid" style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 16 }}>
        {/* Recent Sessions */}
        <div className="card" style={{ marginBottom: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
            <span className="card-title" style={{ marginBottom: 0 }}>Recent Sessions</span>
            <button className="btn-ghost" onClick={() => navigate('sessions')}>View all →</button>
          </div>
          {sessions.length === 0 ? (
            <div className="empty-state" style={{ padding: '24px 0' }}><p>No sessions yet.</p></div>
          ) : sessions.map((s, i) => (
            <div key={s.session_id} onClick={() => navigate('timeline', { sessionId: s.session_id })}
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 0', borderBottom: i < sessions.length - 1 ? '1px solid var(--border)' : 'none', cursor: 'pointer' }}>
              <div>
                <div className="id" style={{ marginBottom: 2 }}>{formatSessionId(s.session_id)}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{s.record_count} records · {displayModel(s.model)}</div>
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--mono)' }}>{timeAgo(s.last_activity)}</div>
            </div>
          ))}
        </div>

        {/* Recent Activity */}
        <div className="card" style={{ marginBottom: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
            <span className="card-title" style={{ marginBottom: 0 }}>Recent Activity</span>
            <button className="btn-ghost" onClick={() => navigate('attempts')}>View all →</button>
          </div>
          {attempts.length === 0 ? (
            <div className="empty-state" style={{ padding: '24px 0' }}><p>No activity yet.</p></div>
          ) : attempts.map((a, i) => (
            <div key={i} onClick={() => a.execution_id && navigate('execution', { executionId: a.execution_id })}
              style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0', borderBottom: i < attempts.length - 1 ? '1px solid var(--border)' : 'none', cursor: a.execution_id ? 'pointer' : 'default' }}>
              <span className={`badge ${dispositionClass(a.disposition)}`}>{dispositionLabel(a.disposition)}</span>
              <span className="mono" style={{ color: 'var(--text-secondary)', minWidth: 80 }}>{displayModel(a.model_id)}</span>
              <span style={{ fontSize: 12, color: 'var(--text-muted)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.path}</span>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-muted)', flexShrink: 0 }}>{timeAgo(a.timestamp)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
