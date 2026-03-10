import { useRef, useEffect } from 'react';

const STEP_COLORS = {
  attestation_ms: '#4ade80',   // green — pass/fail check
  policy_ms: '#4ade80',        // green — policy evaluation
  budget_ms: '#818cf8',        // indigo — budget check
  pre_checks_ms: '#94a3b8',    // slate — aggregate pre-check
  forward_ms: '#60a5fa',       // blue — provider forward
  content_analysis_ms: '#f59e0b', // amber — content analyzers
  chain_ms: '#c084fc',         // purple — Merkle chain
  write_ms: '#f472b6',         // pink — audit write
};

const STEP_LABELS = {
  attestation_ms: 'Attestation',
  policy_ms: 'Policy',
  budget_ms: 'Budget',
  forward_ms: 'Forward',
  content_analysis_ms: 'Content Analysis',
  chain_ms: 'Chain',
  write_ms: 'Write',
};

// Steps to render in order (excluding aggregate keys)
const STEP_ORDER = [
  'attestation_ms', 'policy_ms', 'budget_ms',
  'forward_ms', 'content_analysis_ms',
  'chain_ms', 'write_ms',
];

export default function TraceWaterfall({ timings, toolEvents = [] }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    if (!timings || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;

    // Layout
    const labelWidth = 130;
    const barPadding = 8;
    const rowHeight = 32;
    const rowGap = 4;
    const padding = { top: 40, right: 20, bottom: 20, left: 16 };

    const steps = STEP_ORDER.filter(k => timings[k] != null);
    const totalMs = timings.total_ms || Math.max(...steps.map(k => timings[k] || 0), 1);

    // Tool events as nested rows under forward
    const toolRows = toolEvents.map(te => ({
      label: `  ⚙ ${te.tool_name || 'tool'}`,
      ms: te.duration_ms || 0,
      color: '#eab308', // gold
    }));

    // Build row list
    const rows = [];
    for (const key of steps) {
      rows.push({ label: STEP_LABELS[key] || key, ms: timings[key], color: STEP_COLORS[key] || '#94a3b8', key });
      if (key === 'forward_ms') {
        for (const tr of toolRows) rows.push(tr);
      }
    }

    const height = padding.top + rows.length * (rowHeight + rowGap) + padding.bottom;
    const width = canvas.parentElement?.clientWidth || 600;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    ctx.scale(dpr, dpr);

    // Clear
    ctx.clearRect(0, 0, width, height);

    // Time axis
    const barAreaLeft = padding.left + labelWidth;
    const barAreaWidth = width - barAreaLeft - padding.right;
    const msToX = ms => barAreaLeft + (ms / totalMs) * barAreaWidth;

    // Header
    ctx.fillStyle = '#94a3b8';
    ctx.font = '11px ui-monospace, monospace';
    ctx.textAlign = 'left';
    ctx.fillText('Step', padding.left, padding.top - 12);
    ctx.textAlign = 'right';
    ctx.fillText(`${totalMs.toFixed(0)}ms total`, width - padding.right, padding.top - 12);

    // Grid lines
    const gridSteps = [0.25, 0.5, 0.75, 1.0];
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.15)';
    ctx.lineWidth = 1;
    for (const frac of gridSteps) {
      const x = msToX(totalMs * frac);
      ctx.beginPath();
      ctx.moveTo(x, padding.top);
      ctx.lineTo(x, height - padding.bottom);
      ctx.stroke();
      ctx.fillStyle = 'rgba(148, 163, 184, 0.4)';
      ctx.textAlign = 'center';
      ctx.font = '9px ui-monospace, monospace';
      ctx.fillText(`${(totalMs * frac).toFixed(0)}ms`, x, padding.top - 2);
    }

    // Rows
    let y = padding.top;
    for (const row of rows) {
      // Label
      ctx.fillStyle = '#e2e8f0';
      ctx.font = row.label.startsWith('  ') ? '11px ui-monospace, monospace' : '12px -apple-system, sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText(row.label, barAreaLeft - barPadding, y + rowHeight / 2 + 4);

      // Bar
      const barWidth = Math.max(2, (row.ms / totalMs) * barAreaWidth);
      ctx.fillStyle = row.color;
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      ctx.roundRect(barAreaLeft, y + 4, barWidth, rowHeight - 8, 3);
      ctx.fill();
      ctx.globalAlpha = 1.0;

      // Duration label on bar
      if (barWidth > 40) {
        ctx.fillStyle = '#0f172a';
        ctx.font = 'bold 10px ui-monospace, monospace';
        ctx.textAlign = 'left';
        ctx.fillText(`${row.ms.toFixed(1)}ms`, barAreaLeft + 6, y + rowHeight / 2 + 3);
      } else {
        ctx.fillStyle = '#94a3b8';
        ctx.font = '10px ui-monospace, monospace';
        ctx.textAlign = 'left';
        ctx.fillText(`${row.ms.toFixed(1)}ms`, barAreaLeft + barWidth + 4, y + rowHeight / 2 + 3);
      }

      y += rowHeight + rowGap;
    }
  }, [timings, toolEvents]);

  if (!timings || Object.keys(timings).length === 0) {
    return <div className="text-muted" style={{ padding: 16, fontSize: 12 }}>No timing data available</div>;
  }

  return (
    <div className="trace-waterfall-wrap">
      <canvas ref={canvasRef} className="trace-waterfall" />
    </div>
  );
}
