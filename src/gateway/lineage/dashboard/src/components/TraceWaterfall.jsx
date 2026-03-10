import { useRef, useEffect, useState } from 'react';

const STEP_META = {
  attestation_ms: {
    label: 'Attestation',
    color: '#4ade80',
    desc: 'Verifies the model is registered and approved in the governance registry before allowing inference.',
  },
  policy_ms: {
    label: 'Policy',
    color: '#4ade80',
    desc: 'Evaluates pre-inference policy rules (e.g. allowed models, tenant restrictions, prompt constraints).',
  },
  budget_ms: {
    label: 'Budget',
    color: '#818cf8',
    desc: 'Checks and reserves tokens against the caller\'s daily/monthly token budget limit.',
  },
  pre_checks_ms: {
    label: 'Pre-checks',
    color: '#94a3b8',
    desc: 'Aggregate of all pre-inference checks (attestation + policy + budget). Shown for reference.',
  },
  forward_ms: {
    label: 'LLM Forward',
    color: '#60a5fa',
    desc: 'Time spent calling the LLM provider (Ollama, OpenAI, Anthropic) and receiving the full response.',
  },
  content_analysis_ms: {
    label: 'Content Analysis',
    color: '#f59e0b',
    desc: 'Post-inference content scanning: PII detection, toxicity analysis, and Llama Guard safety classification.',
  },
  chain_ms: {
    label: 'Merkle Chain',
    color: '#c084fc',
    desc: 'Appends this execution to the session\'s Merkle hash chain for tamper-evident audit logging.',
  },
  write_ms: {
    label: 'Audit Write',
    color: '#f472b6',
    desc: 'Persists the execution record to Walacor backend storage and the local Write-Ahead Log (WAL).',
  },
};

const STEP_ORDER = [
  'attestation_ms', 'policy_ms', 'budget_ms',
  'forward_ms', 'content_analysis_ms',
  'chain_ms', 'write_ms',
];

export default function TraceWaterfall({ timings, toolEvents = [] }) {
  const canvasRef = useRef(null);
  const wrapRef = useRef(null);
  const [hoveredStep, setHoveredStep] = useState(null);
  const [tooltipPos, setTooltipPos] = useState({ x: 0, y: 0 });
  const rowMapRef = useRef([]);

  useEffect(() => {
    if (!timings || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;

    // Layout
    const labelWidth = 140;
    const barPadding = 10;
    const rowHeight = 34;
    const rowGap = 5;
    const padding = { top: 44, right: 24, bottom: 24, left: 16 };

    const steps = STEP_ORDER.filter(k => timings[k] != null);
    const totalMs = timings.total_ms || Math.max(...steps.map(k => timings[k] || 0), 1);

    // Tool events as nested rows under forward
    const toolRows = toolEvents.map(te => ({
      label: `  ⚙ ${te.tool_name || 'tool'}`,
      ms: te.duration_ms || 0,
      color: '#eab308',
      key: `tool_${te.tool_name}`,
      desc: `External tool call: ${te.tool_name}. Executed during the LLM forward pass as part of the active tool strategy.`,
    }));

    // Build row list
    const rows = [];
    for (const key of steps) {
      const meta = STEP_META[key] || {};
      rows.push({
        label: meta.label || key,
        ms: timings[key],
        color: meta.color || '#94a3b8',
        key,
        desc: meta.desc || '',
      });
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

    // Store row positions for hit detection
    const rowPositions = [];

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
    ctx.fillText('Pipeline Step', padding.left, padding.top - 14);
    ctx.textAlign = 'right';
    ctx.fillText(`${totalMs.toFixed(0)}ms total`, width - padding.right, padding.top - 14);

    // Grid lines with time markers
    const gridSteps = [0.25, 0.5, 0.75, 1.0];
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.12)';
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
      ctx.fillText(`${(totalMs * frac).toFixed(0)}ms`, x, padding.top - 4);
    }

    // Rows
    let y = padding.top;
    for (const row of rows) {
      const isHovered = hoveredStep === row.key;
      const isTool = row.label.startsWith('  ');

      // Hover highlight
      if (isHovered) {
        ctx.fillStyle = 'rgba(148, 163, 184, 0.06)';
        ctx.fillRect(0, y - 1, width, rowHeight + rowGap + 2);
      }

      // Label
      ctx.fillStyle = isHovered ? '#f8fafc' : '#e2e8f0';
      ctx.font = isTool ? '11px ui-monospace, monospace' : '12px -apple-system, BlinkMacSystemFont, sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText(row.label, barAreaLeft - barPadding, y + rowHeight / 2 + 4);

      // Bar
      const barWidth = Math.max(2, (row.ms / totalMs) * barAreaWidth);
      ctx.fillStyle = row.color;
      ctx.globalAlpha = isHovered ? 1.0 : 0.8;
      ctx.beginPath();
      ctx.roundRect(barAreaLeft, y + 5, barWidth, rowHeight - 10, 3);
      ctx.fill();
      ctx.globalAlpha = 1.0;

      // Duration label
      if (barWidth > 50) {
        ctx.fillStyle = '#0f172a';
        ctx.font = 'bold 10px ui-monospace, monospace';
        ctx.textAlign = 'left';
        ctx.fillText(`${row.ms.toFixed(1)}ms`, barAreaLeft + 8, y + rowHeight / 2 + 3);
      } else {
        ctx.fillStyle = '#94a3b8';
        ctx.font = '10px ui-monospace, monospace';
        ctx.textAlign = 'left';
        ctx.fillText(`${row.ms.toFixed(1)}ms`, barAreaLeft + barWidth + 6, y + rowHeight / 2 + 3);
      }

      // Percentage
      const pct = ((row.ms / totalMs) * 100).toFixed(0);
      if (pct > 0) {
        const pctX = barAreaLeft + barWidth + (barWidth > 50 ? 6 : 40);
        if (pctX + 30 < width) {
          ctx.fillStyle = 'rgba(148, 163, 184, 0.5)';
          ctx.font = '9px ui-monospace, monospace';
          ctx.textAlign = 'left';
          ctx.fillText(`${pct}%`, pctX, y + rowHeight / 2 + 3);
        }
      }

      rowPositions.push({ y, height: rowHeight + rowGap, key: row.key });
      y += rowHeight + rowGap;
    }

    rowMapRef.current = rowPositions;
  }, [timings, toolEvents, hoveredStep]);

  const handleMouseMove = (e) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const mouseY = e.clientY - rect.top;

    let found = null;
    for (const row of rowMapRef.current) {
      if (mouseY >= row.y && mouseY < row.y + row.height) {
        found = row.key;
        break;
      }
    }
    setHoveredStep(found);
    if (found) {
      setTooltipPos({ x: e.clientX, y: e.clientY });
    }
  };

  const handleMouseLeave = () => {
    setHoveredStep(null);
  };

  if (!timings || Object.keys(timings).length === 0) {
    return <div className="text-muted" style={{ padding: 16, fontSize: 12 }}>No timing data available</div>;
  }

  // Find description for hovered step
  const hoveredMeta = hoveredStep
    ? (STEP_META[hoveredStep] || { desc: '' })
    : null;
  const hoveredDesc = hoveredMeta?.desc || '';

  return (
    <div className="trace-waterfall-wrap" ref={wrapRef} style={{ position: 'relative' }}>
      {/* Legend */}
      <div className="trace-legend">
        <div className="trace-legend-item"><span className="trace-dot" style={{ background: '#4ade80' }} />Governance Checks</div>
        <div className="trace-legend-item"><span className="trace-dot" style={{ background: '#818cf8' }} />Budget</div>
        <div className="trace-legend-item"><span className="trace-dot" style={{ background: '#60a5fa' }} />LLM Inference</div>
        <div className="trace-legend-item"><span className="trace-dot" style={{ background: '#f59e0b' }} />Content Safety</div>
        <div className="trace-legend-item"><span className="trace-dot" style={{ background: '#c084fc' }} />Chain Integrity</div>
        <div className="trace-legend-item"><span className="trace-dot" style={{ background: '#f472b6' }} />Audit Storage</div>
        <div className="trace-legend-item"><span className="trace-dot" style={{ background: '#eab308' }} />Tool Execution</div>
      </div>

      <canvas
        ref={canvasRef}
        className="trace-waterfall"
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        style={{ cursor: hoveredStep ? 'help' : 'default' }}
      />

      {/* Tooltip */}
      {hoveredStep && hoveredDesc && (
        <div
          className="trace-tooltip"
          style={{
            position: 'fixed',
            left: tooltipPos.x + 12,
            top: tooltipPos.y - 10,
            zIndex: 1000,
          }}
        >
          <div className="trace-tooltip-title">{STEP_META[hoveredStep]?.label || hoveredStep}</div>
          <div className="trace-tooltip-desc">{hoveredDesc}</div>
        </div>
      )}

      {/* Info note */}
      <div className="trace-info">
        Each bar shows the time spent in that governance pipeline step. Hover over a step for details.
        The pipeline runs sequentially: governance checks → LLM inference → content analysis → chain append → audit write.
      </div>
    </div>
  );
}
