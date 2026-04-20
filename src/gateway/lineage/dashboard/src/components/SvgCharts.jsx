import { useMemo, useRef, useEffect } from 'react';

// Hover handler that coalesces mousemove events to one update per animation
// frame. Without this, a fast sweep across a 1000px wide chart fires ~120
// state updates per second, each triggering a full chart re-memoization
// (paths, xLabels, yTicks all depend on hoverIdx). rAF caps the update rate
// at the monitor refresh and drops redundant intermediate values.
function useRafHover(setHoverIdx, W, padding, dataLen) {
  const rafRef = useRef(0);
  const pendingRef = useRef(null);
  useEffect(() => () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); }, []);
  return (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const xRel = ((e.clientX - rect.left) / rect.width) * W;
    const pct = Math.max(0, Math.min(1, (xRel - padding.left) / (W - padding.left - padding.right)));
    const idx = Math.round(pct * (dataLen - 1));
    pendingRef.current = idx;
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = 0;
      setHoverIdx(pendingRef.current);
    });
  };
}

/* ── Math helpers ─────────────────────────────────────────────────────────── */
function smoothPath(points) {
  if (points.length < 2) return '';
  const p = points;
  let d = `M ${p[0][0].toFixed(2)} ${p[0][1].toFixed(2)}`;
  for (let i = 0; i < p.length - 1; i++) {
    const p0 = p[i - 1] || p[i];
    const p1 = p[i];
    const p2 = p[i + 1];
    const p3 = p[i + 2] || p2;
    const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
    const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
    const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
    const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C ${cp1x.toFixed(2)} ${cp1y.toFixed(2)}, ${cp2x.toFixed(2)} ${cp2y.toFixed(2)}, ${p2[0].toFixed(2)} ${p2[1].toFixed(2)}`;
  }
  return d;
}

function scalePoints(series, width, height, padding, yMax) {
  const { left, right, top, bottom } = padding;
  const w = width - left - right;
  const h = height - top - bottom;
  const n = series.length;
  return series.map((v, i) => {
    const x = left + (n === 1 ? w / 2 : (i / (n - 1)) * w);
    const y = top + h - (v / (yMax || 1)) * h;
    return [x, y];
  });
}

function areaPath(line, padding, height) {
  if (!line.length) return '';
  const y0 = height - padding.bottom;
  const first = line[0];
  const last = line[line.length - 1];
  return `${smoothPath(line)} L ${last[0].toFixed(2)} ${y0} L ${first[0].toFixed(2)} ${y0} Z`;
}

export function chartPalette(isLight) {
  if (isLight) return {
    gold: '#9a6700', green: '#15803d', red: '#dc2626', blue: '#2563eb',
    grid: 'rgba(26,23,20,0.08)', axis: '#9c968c', bg: '#ffffff',
    goldA1: 0.22, goldA2: 0, greenA1: 0.2, greenA2: 0, redA1: 0.18, redA2: 0, blueA1: 0.22, blueA2: 0,
  };
  return {
    gold: '#c9a84c', green: '#34d399', red: '#ef4444', blue: '#60a5fa',
    grid: 'rgba(255,255,255,0.06)', axis: '#65657c', bg: '#08080e',
    goldA1: 0.2, goldA2: 0, greenA1: 0.2, greenA2: 0, redA1: 0.18, redA2: 0, blueA1: 0.22, blueA2: 0,
  };
}

/* ── Throughput Chart ─────────────────────────────────────────────────────── */
export function ThroughputChart({ data, hoverIdx, setHoverIdx, isLight }) {
  const P = chartPalette(isLight);
  const W = 1000, H = 220;
  const padding = { left: 40, right: 12, top: 12, bottom: 26 };
  const id = isLight ? 'light' : 'dark';

  const { paths, xLabels, yTicks, hoverLine } = useMemo(() => {
    if (!data.length) return { paths: [], xLabels: [], yTicks: [], hoverLine: null };
    const rps = data.map(d => d.rps || 0);
    const allowed = data.map(d => d.allowed || 0);
    const blocked = data.map(d => d.blocked || 0);
    const yMaxTotal = Math.max(...allowed.map((a, i) => a + blocked[i])) * 1.1 || 1;
    const yMaxRps = Math.max(...rps) * 1.15 || 1;

    const allowedLine = scalePoints(allowed, W, H, padding, yMaxTotal);
    const blockedLine = scalePoints(blocked, W, H, padding, yMaxTotal);
    const rpsLine = scalePoints(rps, W, H, padding, yMaxRps);

    const yTicks = [];
    for (let i = 0; i <= 3; i++) {
      const v = (yMaxTotal / 3) * i;
      const y = H - padding.bottom - ((v / yMaxTotal) * (H - padding.top - padding.bottom));
      yTicks.push({ y, label: v >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v) });
    }

    const n = data.length;
    const xLabels = [];
    const labelCount = 6;
    for (let i = 0; i <= labelCount; i++) {
      const idx = Math.round((i / labelCount) * (n - 1));
      xLabels.push({
        x: padding.left + (idx / (n - 1)) * (W - padding.left - padding.right),
        text: data[idx]?.t || '',
      });
    }

    const paths = [
      { area: areaPath(allowedLine, padding, H), line: smoothPath(allowedLine), color: P.green, w: 1.5 },
      { area: areaPath(blockedLine, padding, H), line: smoothPath(blockedLine), color: P.red, w: 1.5 },
      { area: null, line: smoothPath(rpsLine), color: P.gold, w: 2.2 },
    ];

    let hoverLine = null;
    if (hoverIdx != null && data[hoverIdx]) {
      const x = padding.left + (hoverIdx / (n - 1)) * (W - padding.left - padding.right);
      hoverLine = { x, d: data[hoverIdx], allowedY: allowedLine[hoverIdx][1], blockedY: blockedLine[hoverIdx][1], rpsY: rpsLine[hoverIdx][1] };
    }

    return { paths, xLabels, yTicks, hoverLine };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, hoverIdx, isLight]);

  const onMove = useRafHover(setHoverIdx, W, padding, data.length);

  return (
    <div className="throughput-chart-wrap" style={{ position: 'relative' }}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           style={{ width: '100%', height: '100%', display: 'block', overflow: 'visible' }}
           onMouseMove={onMove} onMouseLeave={() => setHoverIdx(null)}>
        <defs>
          <linearGradient id={`tg-allowed-${id}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={P.green} stopOpacity={P.greenA1} />
            <stop offset="100%" stopColor={P.green} stopOpacity={P.greenA2} />
          </linearGradient>
          <linearGradient id={`tg-blocked-${id}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={P.red} stopOpacity={P.redA1} />
            <stop offset="100%" stopColor={P.red} stopOpacity={P.redA2} />
          </linearGradient>
        </defs>

        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={padding.left} y1={t.y} x2={W - padding.right} y2={t.y}
                  stroke={P.grid} strokeDasharray="2 4" />
            <text x={padding.left - 6} y={t.y + 3} textAnchor="end"
                  fontFamily="var(--mono)" fontSize="10" fill={P.axis}>{t.label}</text>
          </g>
        ))}
        {xLabels.map((l, i) => (
          <text key={i} x={l.x} y={H - 8} textAnchor="middle"
                fontFamily="var(--mono)" fontSize="10" fill={P.axis}>{l.text}</text>
        ))}

        <path d={paths[0]?.area} fill={`url(#tg-allowed-${id})`} />
        <path d={paths[1]?.area} fill={`url(#tg-blocked-${id})`} />
        {paths.map((p, i) => (
          <path key={i} d={p.line} fill="none" stroke={p.color} strokeWidth={p.w}
                strokeLinecap="round" strokeLinejoin="round" />
        ))}

        {hoverLine && (
          <g>
            <line x1={hoverLine.x} y1={padding.top} x2={hoverLine.x} y2={H - padding.bottom}
                  stroke={P.gold} strokeOpacity="0.5" strokeDasharray="3 3" />
            <circle cx={hoverLine.x} cy={hoverLine.rpsY} r="4" fill={P.gold} stroke={P.bg} strokeWidth="2" />
            <circle cx={hoverLine.x} cy={hoverLine.allowedY} r="3" fill={P.green} stroke={P.bg} strokeWidth="1.5" />
            <circle cx={hoverLine.x} cy={hoverLine.blockedY} r="3" fill={P.red} stroke={P.bg} strokeWidth="1.5" />
          </g>
        )}
      </svg>

      {hoverLine && (
        <div style={{
          position: 'absolute',
          left: `${(hoverLine.x / W) * 100}%`,
          top: 10,
          transform: (hoverLine.x / W) > 0.75 ? 'translateX(calc(-100% - 12px))' : 'translateX(12px)',
          background: 'var(--bg-elevated)',
          border: '1px solid var(--gold-dim)',
          padding: '8px 10px',
          fontFamily: 'var(--mono)',
          fontSize: 10,
          lineHeight: 1.5,
          pointerEvents: 'none',
          minWidth: 140,
          boxShadow: '0 4px 16px var(--shadow, rgba(0,0,0,0.2))',
          color: 'var(--text-primary)',
          zIndex: 10,
        }}>
          <div style={{ color: 'var(--text-muted)', marginBottom: 4, letterSpacing: '0.08em' }}>{hoverLine.d.t}</div>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
            <span style={{ color: P.gold }}>● req/s</span>
            <span style={{ fontWeight: 600 }}>{(hoverLine.d.rps || 0).toFixed(2)}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
            <span style={{ color: P.green }}>● allowed</span>
            <span style={{ fontWeight: 600 }}>{hoverLine.d.allowed}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
            <span style={{ color: P.red }}>● blocked</span>
            <span style={{ fontWeight: 600 }}>{hoverLine.d.blocked}</span>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Token Usage Chart ────────────────────────────────────────────────────── */
export function TokenChart({ data, isLight }) {
  const P = chartPalette(isLight);
  const W = 600, H = 170;
  const padding = { left: 40, right: 10, top: 10, bottom: 22 };
  const id = isLight ? 'light' : 'dark';

  const { paths, xLabels, yTicks } = useMemo(() => {
    if (!data.length) return { paths: [], xLabels: [], yTicks: [] };
    const prompt = data.map(d => d.prompt || 0);
    const completion = data.map(d => d.completion || 0);
    const yMax = Math.max(...prompt, ...completion) * 1.15 || 1;

    const promptLine = scalePoints(prompt, W, H, padding, yMax);
    const completionLine = scalePoints(completion, W, H, padding, yMax);

    const yTicks = [];
    for (let i = 0; i <= 2; i++) {
      const v = (yMax / 2) * i;
      const y = H - padding.bottom - ((v / yMax) * (H - padding.top - padding.bottom));
      yTicks.push({ y, label: v >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v) });
    }

    const n = data.length;
    const xLabels = [];
    for (let i = 0; i <= 4; i++) {
      const idx = Math.round((i / 4) * (n - 1));
      xLabels.push({
        x: padding.left + (idx / (n - 1)) * (W - padding.left - padding.right),
        text: data[idx]?.t || '',
      });
    }

    return {
      paths: [
        { area: areaPath(promptLine, padding, H), line: smoothPath(promptLine), color: P.blue },
        { area: areaPath(completionLine, padding, H), line: smoothPath(completionLine), color: P.gold },
      ],
      xLabels, yTicks,
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, isLight]);

  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: '100%', height: '100%', display: 'block' }}>
        <defs>
          <linearGradient id={`tk-prompt-${id}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={P.blue} stopOpacity={P.blueA1} />
            <stop offset="100%" stopColor={P.blue} stopOpacity="0" />
          </linearGradient>
          <linearGradient id={`tk-completion-${id}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={P.gold} stopOpacity={P.goldA1} />
            <stop offset="100%" stopColor={P.gold} stopOpacity="0" />
          </linearGradient>
        </defs>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={padding.left} y1={t.y} x2={W - padding.right} y2={t.y} stroke={P.grid} strokeDasharray="2 4" />
            <text x={padding.left - 6} y={t.y + 3} textAnchor="end" fontFamily="var(--mono)" fontSize="9" fill={P.axis}>{t.label}</text>
          </g>
        ))}
        {xLabels.map((l, i) => (
          <text key={i} x={l.x} y={H - 6} textAnchor="middle" fontFamily="var(--mono)" fontSize="9" fill={P.axis}>{l.text}</text>
        ))}
        <path d={paths[0]?.area} fill={`url(#tk-prompt-${id})`} />
        <path d={paths[1]?.area} fill={`url(#tk-completion-${id})`} />
        {paths.map((p, i) => (
          <path key={'l' + i} d={p.line} fill="none" stroke={p.color} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        ))}
      </svg>
    </div>
  );
}

/* ── Latency Chart ────────────────────────────────────────────────────────── */
export function LatencyChart({ data, isLight }) {
  const P = chartPalette(isLight);
  const W = 600, H = 170;
  const padding = { left: 44, right: 10, top: 10, bottom: 22 };
  const id = isLight ? 'light' : 'dark';

  const { area, line, xLabels, yTicks, spikeIdx, spike } = useMemo(() => {
    if (!data.length) return { area: '', line: '', xLabels: [], yTicks: [], spikeIdx: null, spike: null };
    const avg = data.map(d => d.avg || 0);
    const yMax = Math.max(...avg) * 1.2 || 1;
    const pts = scalePoints(avg, W, H, padding, yMax);

    const yTicks = [];
    for (let i = 0; i <= 2; i++) {
      const v = (yMax / 2) * i;
      const y = H - padding.bottom - ((v / yMax) * (H - padding.top - padding.bottom));
      yTicks.push({ y, label: Math.round(v) + 'ms' });
    }

    const n = data.length;
    const xLabels = [];
    for (let i = 0; i <= 4; i++) {
      const idx = Math.round((i / 4) * (n - 1));
      xLabels.push({
        x: padding.left + (idx / (n - 1)) * (W - padding.left - padding.right),
        text: data[idx]?.t || '',
      });
    }

    let spikeIdx = 0;
    avg.forEach((v, i) => { if (v > avg[spikeIdx]) spikeIdx = i; });

    return { area: areaPath(pts, padding, H), line: smoothPath(pts), xLabels, yTicks, spikeIdx, spike: pts[spikeIdx] };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, isLight]);

  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: '100%', height: '100%', display: 'block' }}>
        <defs>
          <linearGradient id={`lt-${id}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={P.gold} stopOpacity={P.goldA1} />
            <stop offset="100%" stopColor={P.gold} stopOpacity="0" />
          </linearGradient>
        </defs>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={padding.left} y1={t.y} x2={W - padding.right} y2={t.y} stroke={P.grid} strokeDasharray="2 4" />
            <text x={padding.left - 6} y={t.y + 3} textAnchor="end" fontFamily="var(--mono)" fontSize="9" fill={P.axis}>{t.label}</text>
          </g>
        ))}
        {xLabels.map((l, i) => (
          <text key={i} x={l.x} y={H - 6} textAnchor="middle" fontFamily="var(--mono)" fontSize="9" fill={P.axis}>{l.text}</text>
        ))}
        <path d={area} fill={`url(#lt-${id})`} />
        <path d={line} fill="none" stroke={P.gold} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        {spike && data[spikeIdx] && (
          <g>
            <circle cx={spike[0]} cy={spike[1]} r="4" fill={P.gold} stroke={P.bg} strokeWidth="2" />
            <line x1={spike[0]} y1={spike[1] - 6} x2={spike[0]} y2={padding.top + 2}
                  stroke={P.gold} strokeOpacity="0.5" strokeDasharray="2 2" />
            <text x={spike[0]} y={padding.top} textAnchor="middle" fontFamily="var(--mono)" fontSize="9" fill={P.gold}>
              ↑ {data[spikeIdx].avg}ms
            </text>
          </g>
        )}
      </svg>
    </div>
  );
}
