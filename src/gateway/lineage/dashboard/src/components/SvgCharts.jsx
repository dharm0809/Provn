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

function fmt(n) {
  return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : Math.round(n);
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
/* Bars design. Discrete vertical bar per bucket; allowed is the gold base,
   blocked stacks red on top. Reads as a trading-terminal histogram and
   commits to the design system's brutalist edge (no rounded corners
   anywhere in the system).

   Annotations preserved from the LatencyChart aesthetic:
     • Peak-total spike with dashed riser and "↑ N rps" label
     • Live-tick pulse on the rightmost bar (SVG <animate>, not CSS)
     • Hovered bar highlights; non-hovered bars dim to 0.55 opacity
     • Tooltip still breaks out total / allowed / blocked

   Re-render safety: no CSS animations triggered on mount or data updates.
*/
export function ThroughputChart({ data, hoverIdx, setHoverIdx, isLight }) {
  const P = chartPalette(isLight);
  const W = 600, H = 200;
  const padding = { left: 44, right: 10, top: 22, bottom: 26 };

  // Empty-state — every bucket is zero (e.g. right after a worker restart).
  // Without this the bars vanish and the card looks broken.
  const isFlat = !data.length || data.every(d => !d.allowed && !d.blocked);
  if (isFlat) {
    return (
      <div className="throughput-chart-wrap chart-wrap-empty">
        <div className="chart-empty-text">Waiting for traffic in this window</div>
      </div>
    );
  }

  const memo = useMemo(() => {
    if (!data.length) return null;
    const allowed = data.map(d => d.allowed || 0);
    const blocked = data.map(d => d.blocked || 0);
    const total = allowed.map((a, i) => a + blocked[i]);
    const yMax = Math.max(...total) * 1.15 || 1;

    const innerW = W - padding.left - padding.right;
    const innerH = H - padding.top - padding.bottom;
    const n = data.length;
    const slot = innerW / n;
    const barW = Math.max(2, slot - 1.5);

    const bars = data.map((d, i) => {
      const x = padding.left + i * slot + (slot - barW) / 2;
      const totalH = (total[i] / yMax) * innerH;
      const blockedH = (blocked[i] / yMax) * innerH;
      const allowedH = Math.max(0, totalH - blockedH);
      const yTop = padding.top + innerH - totalH;
      const yAllowedTop = padding.top + innerH - allowedH;
      return { x, w: barW, yTop, yAllowedTop, allowedH, blockedH,
               total: total[i], allowed: allowed[i], blocked: blocked[i] };
    });

    const yTicks = [];
    for (let i = 0; i <= 3; i++) {
      const v = (yMax / 3) * i;
      const y = H - padding.bottom - ((v / yMax) * innerH);
      yTicks.push({ y, label: fmt(v) });
    }

    const xLabels = [];
    for (let i = 0; i <= 5; i++) {
      const idx = Math.round((i / 5) * (n - 1));
      xLabels.push({
        x: padding.left + (idx / (n - 1)) * innerW,
        text: data[idx]?.t || '',
      });
    }

    let spikeIdx = 0;
    total.forEach((v, i) => { if (v > total[spikeIdx]) spikeIdx = i; });

    const lastBar = bars[bars.length - 1];
    const hover = hoverIdx != null && data[hoverIdx]
      ? { bar: bars[hoverIdx], d: data[hoverIdx], idx: hoverIdx }
      : null;

    return {
      bars, yTicks, xLabels, spikeIdx, spikeBar: bars[spikeIdx],
      spikeTotal: total[spikeIdx], lastBar, hover,
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, hoverIdx, isLight]);

  const onMove = useRafHover(setHoverIdx, W, padding, data.length);
  if (!memo) return null;
  const { bars, yTicks, xLabels, spikeIdx, spikeBar, spikeTotal, lastBar, hover } = memo;

  return (
    <div className="throughput-chart-wrap" style={{ position: 'relative' }}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           style={{ width: '100%', height: '100%', display: 'block', overflow: 'visible' }}
           onMouseMove={onMove} onMouseLeave={() => setHoverIdx(null)}>

        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={padding.left} y1={t.y} x2={W - padding.right} y2={t.y}
                  stroke={P.grid} strokeDasharray="2 4" />
            <text x={padding.left - 6} y={t.y + 3} textAnchor="end"
                  fontFamily="var(--mono)" fontSize="9" fill={P.axis}>{t.label}</text>
          </g>
        ))}
        {xLabels.map((l, i) => (
          <text key={i} x={l.x} y={H - 8} textAnchor="middle"
                fontFamily="var(--mono)" fontSize="9" fill={P.axis}>{l.text}</text>
        ))}

        {bars.map((b, i) => (
          <g key={i} opacity={hover && i !== hover.idx ? 0.55 : 1}>
            {b.allowedH > 0 && (
              <rect x={b.x} y={b.yAllowedTop} width={b.w} height={b.allowedH}
                    fill={P.gold} fillOpacity={0.85} />
            )}
            {b.blockedH > 0 && (
              <rect x={b.x} y={b.yTop} width={b.w} height={b.blockedH}
                    fill={P.red} fillOpacity={0.95} />
            )}
          </g>
        ))}

        {/* Peak-total spike annotation */}
        {spikeBar && data[spikeIdx] && (
          <g>
            <line x1={spikeBar.x + spikeBar.w / 2} y1={spikeBar.yTop - 4}
                  x2={spikeBar.x + spikeBar.w / 2} y2={padding.top + 2}
                  stroke={P.gold} strokeOpacity="0.5" strokeDasharray="2 2" />
            <text x={spikeBar.x + spikeBar.w / 2} y={padding.top - 4} textAnchor="middle"
                  fontFamily="var(--mono)" fontSize="9" fill={P.gold}>
              ↑ {fmt(spikeTotal)} rps
            </text>
          </g>
        )}

        {/* Last bar — gold outline frame to mark "now"; live-tick dot above.
            SVG <animate> is continuous and not bound to React reconciliation. */}
        {lastBar && (
          <g>
            <rect x={lastBar.x - 1} y={lastBar.yTop - 1} width={lastBar.w + 2}
                  height={(H - padding.bottom) - (lastBar.yTop - 1)}
                  fill="none" stroke={P.gold} strokeOpacity="0.6" strokeWidth="0.8" />
            <circle cx={lastBar.x + lastBar.w / 2} cy={lastBar.yTop - 6} r="2.5" fill={P.gold}>
              <animate attributeName="r" values="2.5;5;2.5" dur="2s" repeatCount="indefinite" />
              <animate attributeName="fill-opacity" values="1;0.2;1" dur="2s" repeatCount="indefinite" />
            </circle>
          </g>
        )}

        {/* Hover highlight ring */}
        {hover && (
          <rect x={hover.bar.x - 0.5} y={hover.bar.yTop - 0.5}
                width={hover.bar.w + 1}
                height={(H - padding.bottom) - (hover.bar.yTop - 0.5)}
                fill="none" stroke={P.gold} strokeWidth="1" />
        )}
      </svg>

      {hover && (
        <div style={{
          position: 'absolute',
          left: `${((hover.bar.x + hover.bar.w / 2) / W) * 100}%`,
          top: 10,
          transform: ((hover.bar.x + hover.bar.w / 2) / W) > 0.75
            ? 'translateX(calc(-100% - 12px))'
            : 'translateX(12px)',
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
          <div style={{ color: 'var(--text-muted)', marginBottom: 4, letterSpacing: '0.08em' }}>{hover.d.t}</div>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
            <span style={{ color: P.gold }}>● total</span>
            <span style={{ fontWeight: 600 }}>{(hover.d.allowed || 0) + (hover.d.blocked || 0)}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
            <span style={{ color: P.gold, opacity: 0.85 }}>● allowed</span>
            <span style={{ fontWeight: 600 }}>{hover.d.allowed}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
            <span style={{ color: P.red }}>● blocked</span>
            <span style={{ fontWeight: 600 }}>{hover.d.blocked}</span>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── Token Usage Chart ────────────────────────────────────────────────────── */
/* Bars design — same vocabulary as ThroughputChart. The prompt/completion
   split lives in the twin-summary stats below the card, so the chart
   itself stays single-color (gold bars for total per bucket).
*/
export function TokenChart({ data, isLight }) {
  const P = chartPalette(isLight);
  const W = 600, H = 170;
  const padding = { left: 44, right: 10, top: 20, bottom: 22 };

  const isFlat = !data.length || data.every(d => !d.prompt && !d.completion);
  if (isFlat) {
    return (
      <div className="chart-wrap chart-wrap-empty">
        <div className="chart-empty-text">Waiting for token activity in this window</div>
      </div>
    );
  }

  const memo = useMemo(() => {
    if (!data.length) return null;
    const total = data.map(d => (d.prompt || 0) + (d.completion || 0));
    const yMax = Math.max(...total) * 1.15 || 1;

    const innerW = W - padding.left - padding.right;
    const innerH = H - padding.top - padding.bottom;
    const n = data.length;
    const slot = innerW / n;
    const barW = Math.max(2, slot - 1.5);

    const bars = data.map((d, i) => {
      const x = padding.left + i * slot + (slot - barW) / 2;
      const h = (total[i] / yMax) * innerH;
      const yTop = padding.top + innerH - h;
      return { x, w: barW, yTop, h, total: total[i] };
    });

    const yTicks = [];
    for (let i = 0; i <= 2; i++) {
      const v = (yMax / 2) * i;
      const y = H - padding.bottom - ((v / yMax) * innerH);
      yTicks.push({ y, label: fmt(v) });
    }

    const xLabels = [];
    for (let i = 0; i <= 4; i++) {
      const idx = Math.round((i / 4) * (n - 1));
      xLabels.push({
        x: padding.left + (idx / (n - 1)) * innerW,
        text: data[idx]?.t || '',
      });
    }

    let spikeIdx = 0;
    total.forEach((v, i) => { if (v > total[spikeIdx]) spikeIdx = i; });

    return {
      bars, yTicks, xLabels, spikeIdx,
      spikeBar: bars[spikeIdx], spikeTotal: total[spikeIdx],
      lastBar: bars[bars.length - 1],
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, isLight]);

  if (!memo) return null;
  const { bars, yTicks, xLabels, spikeIdx, spikeBar, spikeTotal, lastBar } = memo;

  return (
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           style={{ width: '100%', height: '100%', display: 'block' }}>
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={padding.left} y1={t.y} x2={W - padding.right} y2={t.y}
                  stroke={P.grid} strokeDasharray="2 4" />
            <text x={padding.left - 6} y={t.y + 3} textAnchor="end"
                  fontFamily="var(--mono)" fontSize="9" fill={P.axis}>{t.label}</text>
          </g>
        ))}
        {xLabels.map((l, i) => (
          <text key={i} x={l.x} y={H - 6} textAnchor="middle"
                fontFamily="var(--mono)" fontSize="9" fill={P.axis}>{l.text}</text>
        ))}

        {bars.map((b, i) => (
          <rect key={i} x={b.x} y={b.yTop} width={b.w} height={b.h}
                fill={P.gold} fillOpacity={0.82} />
        ))}

        {spikeBar && data[spikeIdx] && (
          <g>
            <line x1={spikeBar.x + spikeBar.w / 2} y1={spikeBar.yTop - 4}
                  x2={spikeBar.x + spikeBar.w / 2} y2={padding.top + 2}
                  stroke={P.gold} strokeOpacity="0.5" strokeDasharray="2 2" />
            <text x={spikeBar.x + spikeBar.w / 2} y={padding.top - 4} textAnchor="middle"
                  fontFamily="var(--mono)" fontSize="9" fill={P.gold}>
              ↑ {fmt(spikeTotal)} tok
            </text>
          </g>
        )}

        {lastBar && (
          <g>
            <rect x={lastBar.x - 1} y={lastBar.yTop - 1} width={lastBar.w + 2}
                  height={(H - padding.bottom) - (lastBar.yTop - 1)}
                  fill="none" stroke={P.gold} strokeOpacity="0.6" strokeWidth="0.8" />
            <circle cx={lastBar.x + lastBar.w / 2} cy={lastBar.yTop - 6} r="2.5" fill={P.gold}>
              <animate attributeName="r" values="2.5;5;2.5" dur="2s" repeatCount="indefinite" />
              <animate attributeName="fill-opacity" values="1;0.2;1" dur="2s" repeatCount="indefinite" />
            </circle>
          </g>
        )}
      </svg>
    </div>
  );
}

/* ── Latency Chart ────────────────────────────────────────────────────────── */
/* Unchanged — this is the approved reference design. */
export function LatencyChart({ data, isLight }) {
  const P = chartPalette(isLight);
  const W = 600, H = 170;
  const padding = { left: 44, right: 10, top: 10, bottom: 22 };
  const id = isLight ? 'light' : 'dark';

  const { area, line, xLabels, yTicks, spikeIdx, spike, lastPt } = useMemo(() => {
    if (!data.length) return { area: '', line: '', xLabels: [], yTicks: [], spikeIdx: null, spike: null, lastPt: null };
    const avg = data.map(d => d.avg || 0);
    const yMax = Math.max(...avg) * 1.2 || 1;
    const pts = scalePoints(avg, W, H, padding, yMax);
    const lastPt = pts[pts.length - 1] || null;

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

    return { area: areaPath(pts, padding, H), line: smoothPath(pts), xLabels, yTicks, spikeIdx, spike: pts[spikeIdx], lastPt };
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
        {lastPt && lastPt[0] !== spike?.[0] && (
          <g>
            <circle cx={lastPt[0]} cy={lastPt[1]} r="3" fill={P.gold}>
              <animate attributeName="r" values="3;6;3" dur="2s" repeatCount="indefinite" />
              <animate attributeName="fill-opacity" values="1;0.2;1" dur="2s" repeatCount="indefinite" />
            </circle>
            <circle cx={lastPt[0]} cy={lastPt[1]} r="2" fill={P.gold} />
          </g>
        )}
      </svg>
    </div>
  );
}
