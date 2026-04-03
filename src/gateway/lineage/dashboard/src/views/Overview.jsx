import { useState, useEffect } from 'react';
import Chart from 'react-apexcharts';
import { getSessions, getAttempts, getThroughputHistory, getTokenLatency } from '../api';
import { timeAgo, formatNumber, formatUptime, displayModel, formatSessionId, dispositionClass, dispositionLabel } from '../utils';

const POLL_MS = 3000;
const RANGE_SECONDS = { '1h': 3600, '24h': 86400, '7d': 604800, '30d': 2592000 };
const AXIS_LABEL_FONT = '12px';
const MONO = '"IBM Plex Mono", Menlo, monospace';

/** Tracks `data-theme="light"` on <html> (same as App sidebar toggle). */
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

function chartPalette(isLight) {
  if (isLight) {
    return {
      themeMode: 'light',
      tooltipTheme: 'light',
      axisColor: '#57524a',
      gridBorder: 'rgba(26, 23, 20, 0.14)',
      crosshair: 'rgba(26, 23, 20, 0.2)',
      gold: '#9a6700',
      green: '#15803d',
      red: '#dc2626',
      blue: '#2563eb',
    };
  }
  return {
    themeMode: 'dark',
    tooltipTheme: 'dark',
    axisColor: '#8b8ba8',
    gridBorder: 'rgba(255, 255, 255, 0.1)',
    crosshair: 'rgba(255, 255, 255, 0.16)',
    gold: '#c9a84c',
    green: '#34d399',
    red: '#ef4444',
    blue: '#6366f1',
  };
}

function axisLabelStyle(palette) {
  return { colors: palette.axisColor, fontSize: AXIS_LABEL_FONT, fontFamily: MONO };
}

/** Merge chart options without losing `toolbar: false` when callers pass `chart.zoom`. */
function baseChartOptions(palette, overrides = {}) {
  const { chart: chartOverrides = {}, ...rest } = overrides;
  return {
    chart: {
      background: 'transparent',
      toolbar: {
        show: false,
        tools: {
          download: false,
          selection: false,
          zoom: false,
          zoomin: false,
          zoomout: false,
          pan: false,
          reset: false,
        },
      },
      zoom: { enabled: false },
      fontFamily: MONO,
      animations: {
        enabled: true,
        easing: 'easeinout',
        speed: 600,
        dynamicAnimation: { enabled: true, speed: 400 },
      },
      ...chartOverrides,
    },
    theme: { mode: palette.themeMode },
    grid: {
      borderColor: palette.gridBorder,
      strokeDashArray: 4,
      xaxis: { lines: { show: false } },
      yaxis: { lines: { show: true } },
      padding: { left: 6, right: 8 },
    },
    tooltip: {
      theme: palette.tooltipTheme,
      style: { fontSize: '12px', fontFamily: MONO },
      x: { show: true },
    },
    stroke: { curve: 'smooth', width: 2 },
    dataLabels: { enabled: false },
    legend: { show: false },
    ...rest,
  };
}

function xAxisCommon(palette, partial) {
  return {
    axisBorder: { show: false },
    axisTicks: { show: false },
    crosshairs: { show: true, stroke: { color: palette.crosshair, width: 1, dashArray: 4 } },
    tooltip: { enabled: false },
    ...partial,
    labels: { style: axisLabelStyle(palette), ...partial.labels },
  };
}

// ─── Throughput Historical Chart ──────────────────────────────────────────────
function ThroughputHistoricalChart({ data, palette }) {
  const series = [
    { name: 'req/s', data: data.map(d => d.rps || 0) },
    { name: 'allowed', data: data.map(d => d.allowed || 0) },
    { name: 'blocked', data: data.map(d => d.blocked || 0) },
  ];
  const categories = data.map(d => d.t || '');

  const options = baseChartOptions(palette, {
    chart: {
      type: 'area',
      height: 300,
      zoom: { enabled: true, type: 'x', allowMouseWheelZoom: true, autoScaleYaxis: true },
      selection: {
        enabled: true,
        type: 'x',
        fill: { color: palette.axisColor, opacity: 0.12 },
        stroke: { width: 1, color: palette.gold, opacity: 0.55, dashArray: 4 },
      },
    },
    colors: [palette.gold, palette.green, palette.red],
    fill: {
      type: 'gradient',
      gradient: { shadeIntensity: 1, opacityFrom: 0.12, opacityTo: 0.0, stops: [0, 90, 100] },
    },
    stroke: { curve: 'smooth', width: [2, 1.5, 1.5] },
    xaxis: xAxisCommon(palette, {
      categories,
      tickAmount: 8,
      labels: { rotate: -45, rotateAlways: false, hideOverlappingLabels: true, maxHeight: 52 },
    }),
    yaxis: {
      labels: {
        style: axisLabelStyle(palette),
        formatter: (v) => v < 1 ? v.toFixed(2) : Math.round(v),
      },
    },
    tooltip: {
      shared: true,
      intersect: false,
      y: { formatter: (v) => v != null ? v.toFixed(2) : '0' },
    },
  });

  return <Chart options={options} series={series} type="area" height={300} />;
}

// ─── Token Usage Chart ────────────────────────────────────────────────────────
function TokenUsageChart({ data, palette }) {
  const series = [
    { name: 'prompt', data: data.map(d => d.prompt || 0) },
    { name: 'completion', data: data.map(d => d.completion || 0) },
  ];
  const categories = data.map(d => d.t || '');

  const options = baseChartOptions(palette, {
    chart: {
      type: 'area',
      height: 240,
      zoom: { enabled: true, type: 'x', allowMouseWheelZoom: true, autoScaleYaxis: true },
      selection: {
        enabled: true,
        type: 'x',
        fill: { color: palette.axisColor, opacity: 0.12 },
        stroke: { width: 1, color: palette.gold, opacity: 0.55, dashArray: 4 },
      },
    },
    colors: [palette.blue, palette.gold],
    fill: {
      type: 'gradient',
      gradient: { shadeIntensity: 1, opacityFrom: 0.2, opacityTo: 0.0, stops: [0, 95, 100] },
    },
    stroke: { curve: 'smooth', width: [2, 2] },
    xaxis: xAxisCommon(palette, {
      categories,
      tickAmount: 8,
      labels: { rotate: -45, rotateAlways: false, hideOverlappingLabels: true, maxHeight: 52 },
    }),
    yaxis: {
      labels: {
        style: axisLabelStyle(palette),
        formatter: (v) => v >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v),
      },
    },
    tooltip: {
      shared: true,
      intersect: false,
      y: { formatter: (v) => v != null ? Math.round(v) + ' tok' : '0' },
    },
    markers: { size: 0, hover: { size: 4, sizeOffset: 2 } },
  });

  return <Chart options={options} series={series} type="area" height={240} />;
}

// ─── Latency Chart ────────────────────────────────────────────────────────────
function LatencyChart({ data, palette }) {
  const series = [{ name: 'avg latency', data: data.map(d => Math.round(d.avg || 0)) }];
  const categories = data.map(d => d.t || '');

  const options = baseChartOptions(palette, {
    chart: {
      type: 'area',
      height: 240,
      zoom: { enabled: true, type: 'x', allowMouseWheelZoom: true, autoScaleYaxis: true },
      selection: {
        enabled: true,
        type: 'x',
        fill: { color: palette.axisColor, opacity: 0.12 },
        stroke: { width: 1, color: palette.gold, opacity: 0.55, dashArray: 4 },
      },
    },
    colors: [palette.gold],
    fill: {
      type: 'gradient',
      gradient: { shadeIntensity: 1, opacityFrom: 0.15, opacityTo: 0.0, stops: [0, 95, 100] },
    },
    stroke: { curve: 'smooth', width: [2.5] },
    xaxis: xAxisCommon(palette, {
      categories,
      tickAmount: 8,
      labels: { rotate: -45, rotateAlways: false, hideOverlappingLabels: true, maxHeight: 52 },
    }),
    yaxis: {
      labels: {
        style: axisLabelStyle(palette),
        formatter: (v) => Math.round(v) + 'ms',
      },
    },
    tooltip: {
      shared: true,
      intersect: false,
      y: { formatter: (v) => v != null ? Math.round(v) + ' ms' : '--' },
    },
    markers: { size: 0, hover: { size: 4, sizeOffset: 2 } },
  });

  return <Chart options={options} series={series} type="area" height={240} />;
}

// ─── Range Selector ───────────────────────────────────────────────────────────
function RangeSelector({ active, onChange }) {
  const opts = [
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

// ─── Compact Status Strip ─────────────────────────────────────────────────────
function StatusStrip({ health, sessionsCount, attTotal, pctAllowed }) {
  const ok = health?.status === 'healthy';
  const divider = <div style={{ width: 1, height: 18, background: 'var(--border)', flexShrink: 0 }} />;
  return (
    <div className="card card-accent-green" style={{ padding: '12px 20px', marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 20, flexWrap: 'wrap' }}>
        {/* Health dot + label */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
          <span style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
            <span style={{ position: 'absolute', width: 14, height: 14, borderRadius: '50%', backgroundColor: ok ? 'var(--green)' : 'var(--amber)', opacity: 0.35, animation: 'ping 2s cubic-bezier(0,0,0.2,1) infinite' }} />
            <span style={{ position: 'relative', width: 9, height: 9, borderRadius: '50%', backgroundColor: ok ? 'var(--green)' : 'var(--amber)' }} />
          </span>
          <span style={{ fontFamily: 'var(--mono)', fontSize: 13, fontWeight: 700, color: ok ? 'var(--green)' : 'var(--amber)', letterSpacing: '0.5px' }}>
            {ok ? 'ALL CLEAR' : (health?.status || 'OFFLINE').toUpperCase()}
          </span>
        </div>

        {divider}

        {/* Request stats */}
        {[
          [sessionsCount, 'sessions'],
          [formatNumber(attTotal), 'requests'],
          [pctAllowed + '%', 'allowed'],
        ].map(([val, label], i) => (
          <div key={i} style={{ fontSize: 12, color: 'var(--text-secondary)', fontFamily: 'var(--mono)', whiteSpace: 'nowrap' }}>
            <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{val}</span>
            <span style={{ margin: '0 5px', opacity: 0.3 }}>·</span>
            <span style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.4px' }}>{label}</span>
          </div>
        ))}

        {divider}

        {/* Governance flags */}
        {[
          [health?.enforcement_mode === 'enforced', health?.enforcement_mode || '-', 'mode'],
          [!!health?.content_analyzers, `${health?.content_analyzers ?? 0}`, 'analyzers'],
          [!!health?.session_chain, health?.session_chain ? 'enabled' : 'disabled', 'chain'],
          [ok, health?.uptime_seconds != null ? formatUptime(health.uptime_seconds) : '-', 'uptime'],
        ].map(([isOk, val, label], i) => (
          <div key={i} style={{ fontSize: 11, color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
            <span style={{ fontFamily: 'var(--mono)', color: isOk ? 'var(--green)' : 'var(--text-primary)' }}>{val}</span>
            <span style={{ margin: '0 4px', opacity: 0.3 }}>·</span>
            <span style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.4px' }}>{label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── Chart empty/loading placeholder ─────────────────────────────────────────
function ChartPlaceholder({ height, message, sub }) {
  return (
    <div style={{ height, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
      <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-secondary)' }}>{message}</span>
      {sub && <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{sub}</span>}
    </div>
  );
}

// ─── Main Overview ───────────────────────────────────────────────────────────
export default function Overview({ navigate, health }) {
  const isLight = useLineageTheme();
  const palette = chartPalette(isLight);

  const [sessions, setSessions] = useState([]);
  const [attempts, setAttempts] = useState([]);
  const [attStats, setAttStats] = useState({});
  const [attTotal, setAttTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [chartRange, setChartRange] = useState('1h');
  const [tpData, setTpData] = useState([]);
  const [tpLoading, setTpLoading] = useState(false);
  const [tkData, setTkData] = useState([]);
  const [ltData, setLtData] = useState([]);
  const [counters, setCounters] = useState({ rps: 0, tps: 0, pct: 100, total: 0 });
  const [tokenSnap, setTokenSnap] = useState({ prompt: 0, completion: 0 });
  const [latencySnap, setLatencySnap] = useState({ avg: 0 });

  // Recent sessions + activity: initial load then same cadence as charts (no loading flash on refresh)
  useEffect(() => {
    let cancelled = false;
    const refresh = async (isFirst) => {
      try {
        const [sessData, attData] = await Promise.all([getSessions(6, 0), getAttempts(8, 0)]);
        if (cancelled) return;
        setSessions(sessData.sessions || []);
        setAttempts(attData.items || []);
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
    const id = setInterval(() => refresh(false), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Throughput + token usage + latency: one range for all charts (WAL bucket APIs). 1H refreshes on POLL_MS.
  useEffect(() => {
    const needsDate = chartRange === '7d' || chartRange === '30d';
    const label = (t) => (t ? (needsDate ? t.substring(5, 16).replace('T', ' ') : t.substring(11, 16)) : '');
    const mapThroughput = (d) => (d.buckets || []).map(b => ({
      t: label(b.t),
      rps: b.request_count ?? b.total ?? 0,
      allowed: b.allowed || 0,
      blocked: b.blocked != null ? b.blocked : Math.max(0, (b.request_count ?? b.total ?? 0) - (b.allowed || 0)),
    }));
    const mapTkLt = (d) => (d.buckets || []).map(b => ({
      t: label(b.t),
      prompt: b.prompt_tokens ?? 0,
      completion: b.completion_tokens ?? 0,
      avg: b.avg_latency_ms ?? 0,
      count: b.request_count ?? 0,
    }));

    const applySummary = (tpRows, tkRows) => {
      const total = tpRows.reduce((s, d) => s + (d.rps || 0), 0);
      const allowed = tpRows.reduce((s, d) => s + (d.allowed || 0), 0);
      const secs = RANGE_SECONDS[chartRange] || 3600;
      const rps = secs > 0 ? total / secs : 0;
      const promptSum = tkRows.reduce((s, d) => s + (d.prompt || 0), 0);
      const compSum = tkRows.reduce((s, d) => s + (d.completion || 0), 0);
      const tkTotal = promptSum + compSum;
      const tps = secs > 0 ? tkTotal / secs : 0;
      const pct = total > 0 ? (allowed / total * 100) : 100;
      setCounters({ rps, tps, pct, total });
      setTokenSnap({ prompt: promptSum, completion: compSum });
      let wSum = 0;
      let wCount = 0;
      for (const d of tkRows) {
        const c = d.count || 0;
        if (c > 0) {
          wSum += (d.avg || 0) * c;
          wCount += c;
        }
      }
      setLatencySnap({ avg: wCount > 0 ? wSum / wCount : 0 });
    };

    let cancelled = false;
    const loadCharts = async (isFirst) => {
      if (isFirst) {
        setTpLoading(true);
        setTpData([]);
        setTkData([]);
        setLtData([]);
      }
      try {
        const [td, tld] = await Promise.all([
          getThroughputHistory(chartRange),
          getTokenLatency(chartRange),
        ]);
        if (cancelled) return;
        const tpRows = mapThroughput(td);
        const tkRows = mapTkLt(tld);
        setTpData(tpRows);
        setTkData(tkRows);
        setLtData(tkRows.map(({ t, avg }) => ({ t, avg })));
        applySummary(tpRows, tkRows);
      } catch { /* keep prior series on refresh failure */ }
      if (!cancelled && isFirst) setTpLoading(false);
    };

    loadCharts(true);
    if (chartRange === '1h') {
      const id = setInterval(() => { if (!cancelled) loadCharts(false); }, POLL_MS);
      return () => {
        cancelled = true;
        clearInterval(id);
      };
    }
    return () => { cancelled = true; };
  }, [chartRange]);

  if (loading) return (
    <div>
      <div className="skeleton-block" style={{ height: 48, marginBottom: 16 }} />
      <div className="skeleton-block" style={{ height: 360, marginBottom: 16 }} />
      <div style={{ display: 'flex', gap: 16, marginBottom: 16 }}>
        <div className="skeleton-block" style={{ flex: 1, height: 300 }} />
        <div className="skeleton-block" style={{ flex: 1, height: 300 }} />
      </div>
    </div>
  );
  if (error) return <div className="error-card">Error: {error}</div>;

  const allowed = attStats.allowed || 0;
  const pctAllowed = attTotal > 0 ? (allowed / attTotal * 100).toFixed(1) : '100';

  const showThroughputChart = tpData.length > 0;
  const showTkLtCharts = tkData.length > 0;

  return (
    <div className="fade-child">

      {/* ── Compact Status Strip ── */}
      <StatusStrip
        health={health}
        sessionsCount={sessions.length}
        attTotal={attTotal}
        pctAllowed={pctAllowed}
      />

      {/* ── Shared time range (all telemetry charts) ── */}
      <div className="card" style={{ marginBottom: 16, padding: '12px 18px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {chartRange === '1h' && (
            <div
              style={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                background: palette.gold,
                boxShadow: isLight ? `0 0 6px ${palette.gold}55` : `0 0 8px rgba(201,168,76,0.45)`,
                animation: 'pulse 1.5s ease-in-out infinite',
              }}
              title="Charts refresh every few seconds for 1H"
            />
          )}
          <span className="card-title" style={{ marginBottom: 0 }}>Time range</span>
          <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--mono)' }}>throughput · tokens · latency</span>
        </div>
        <RangeSelector active={chartRange} onChange={setChartRange} />
      </div>

      {/* ── Throughput Chart ── */}
      <div className="card card-accent" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <span className="card-title" style={{ marginBottom: 0 }}>Throughput</span>
          <div style={{ display: 'flex', gap: 12 }}>
            {[['req/s', palette.gold], ['allowed', palette.green], ['blocked', palette.red]].map(([l, c]) => (
              <div key={l} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--mono)' }}>
                <span style={{ width: 20, height: 2, background: c, display: 'inline-block', borderRadius: 1 }} />
                {l}
              </div>
            ))}
          </div>
        </div>

        {tpLoading ? (
          <ChartPlaceholder height={300} message="loading…" />
        ) : showThroughputChart ? (
          <>
            <ThroughputHistoricalChart data={tpData} palette={palette} />
            <p className="chart-zoom-hint">
              <span className="chart-zoom-hint-label">Zoom and explore</span>
              All charts use the same time range. Hover and <strong>scroll</strong> (or <strong>pinch</strong>) to zoom the
              time axis; <strong>drag</strong> to select a span. To reset zoom, pick another range above, then switch back.
            </p>
          </>
        ) : (
          <ChartPlaceholder height={300} message="no data for this range" sub="no attempts in the selected window" />
        )}

        {/* Counter strip */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginTop: 12, paddingTop: 14, borderTop: '1px solid var(--border)' }}>
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

      {/* ── Token Usage + Latency Charts ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 16 }}>

        {/* Token Usage */}
        <div className="card" style={{ marginBottom: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
            <span className="card-title" style={{ marginBottom: 0 }}>Token Usage</span>
            <div style={{ display: 'flex', gap: 16, fontSize: 11, fontFamily: 'var(--mono)' }}>
              <span style={{ color: 'var(--text-muted)' }}>
                <span style={{ color: 'var(--blue)' }}>P</span> {formatNumber(tokenSnap.prompt)}
              </span>
              <span style={{ color: 'var(--text-muted)' }}>
                <span style={{ color: 'var(--gold)' }}>C</span> {formatNumber(tokenSnap.completion)}
              </span>
              <span style={{ color: 'var(--text-secondary)', fontWeight: 600 }}>
                {formatNumber(tokenSnap.prompt + tokenSnap.completion)}
              </span>
            </div>
          </div>
          {showTkLtCharts ? (
            <TokenUsageChart data={tkData} palette={palette} />
          ) : (
            <ChartPlaceholder height={240} message={tpLoading ? 'loading…' : 'no token data for this range'} sub="execution records with token fields appear here" />
          )}
        </div>

        {/* Latency */}
        <div className="card" style={{ marginBottom: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
            <span className="card-title" style={{ marginBottom: 0 }}>Latency</span>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 14, fontWeight: 600, color: latencySnap.avg > 0 ? 'var(--gold)' : 'var(--text-muted)' }}>
              {latencySnap.avg > 0 ? Math.round(latencySnap.avg) + ' ms' : '--'}
              <span style={{ fontSize: 9, color: 'var(--text-muted)', fontWeight: 400, marginLeft: 4 }}>avg</span>
            </div>
          </div>
          {showTkLtCharts ? (
            <LatencyChart data={ltData} palette={palette} />
          ) : (
            <ChartPlaceholder height={240} message={tpLoading ? 'loading…' : 'no latency data for this range'} sub="execution records with latency_ms appear here" />
          )}
        </div>
      </div>

      {/* ── Sessions + Activity ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.4fr', gap: 16 }}>
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
