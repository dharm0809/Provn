import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import * as api from '../api';

const CATEGORY_ORDER = ['security', 'integrity', 'persistence', 'dependency', 'feature', 'hygiene'];
const CATEGORY_LABEL = {
  security: 'Security',
  integrity: 'Integrity',
  persistence: 'Persistence',
  dependency: 'Dependencies',
  feature: 'Feature coherence',
  hygiene: 'Hygiene',
};

const STATUS_COLOR = {
  green: 'var(--green)',
  amber: 'var(--amber)',
  red: 'var(--red)',
};

const ROLLUP_COLOR = {
  ready: 'var(--green)',
  degraded: 'var(--amber)',
  unready: 'var(--red)',
};

const REFRESH_MS = 60_000;

function StatusDot({ status }) {
  return (
    <span
      style={{
        display: 'inline-block',
        width: 10,
        height: 10,
        borderRadius: '50%',
        backgroundColor: STATUS_COLOR[status] || 'var(--text-muted)',
        boxShadow: `0 0 6px ${STATUS_COLOR[status] || 'var(--text-muted)'}`,
        flexShrink: 0,
      }}
    />
  );
}

function CheckRow({ check }) {
  const [open, setOpen] = useState(false);
  const hasDetail = !!(check.remediation || (check.evidence && Object.keys(check.evidence).length > 0));

  return (
    <div style={{ borderBottom: '1px solid var(--border)', padding: '10px 0' }}>
      <div
        onClick={() => hasDetail && setOpen(o => !o)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          cursor: hasDetail ? 'pointer' : 'default',
        }}
      >
        <StatusDot status={check.status} />
        <span className="mono" style={{ fontSize: 12, color: 'var(--text-muted)', width: 64, flexShrink: 0 }}>
          {check.id}
        </span>
        <span style={{ fontSize: 13, fontWeight: 600, flexShrink: 0, width: 200 }}>{check.name}</span>
        <span style={{ fontSize: 12, color: 'var(--text-secondary)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {check.detail}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)', flexShrink: 0 }}>
          {check.elapsed_ms}ms
        </span>
        {hasDetail && (
          <span style={{ fontSize: 10, color: 'var(--text-muted)', width: 12, flexShrink: 0 }}>
            {open ? '▼' : '▶'}
          </span>
        )}
      </div>
      {open && hasDetail && (
        <div style={{ marginTop: 10, marginLeft: 22, fontSize: 12, lineHeight: 1.6 }}>
          {check.remediation && (
            <div style={{ marginBottom: 8 }}>
              <span style={{ color: 'var(--gold)', fontWeight: 600 }}>Remediation: </span>
              <span style={{ color: 'var(--text-primary)' }}>{check.remediation}</span>
            </div>
          )}
          {check.evidence && Object.keys(check.evidence).length > 0 && (
            <pre className="mono" style={{
              margin: 0,
              padding: 10,
              backgroundColor: 'var(--bg-inset)',
              fontSize: 11,
              color: 'var(--text-secondary)',
              overflow: 'auto',
              maxHeight: 300,
              border: '1px solid var(--border)',
            }}>
              {JSON.stringify(check.evidence, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function CategoryGroup({ category, checks }) {
  const counts = useMemo(() => {
    const c = { green: 0, amber: 0, red: 0 };
    for (const chk of checks) c[chk.status] = (c[chk.status] || 0) + 1;
    return c;
  }, [checks]);

  return (
    <section
      className="status-section"
      style={{ marginBottom: 16, padding: 16, border: '1px solid var(--border)', backgroundColor: 'var(--bg-surface)' }}
    >
      <h3 className="status-section-title" style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 0, marginBottom: 8 }}>
        <span>{CATEGORY_LABEL[category] || category}</span>
        <span style={{ fontSize: 12, color: 'var(--text-muted)', fontWeight: 400 }}>
          {counts.green > 0 && <span style={{ color: 'var(--green)', marginRight: 8 }}>{counts.green} green</span>}
          {counts.amber > 0 && <span style={{ color: 'var(--amber)', marginRight: 8 }}>{counts.amber} amber</span>}
          {counts.red > 0 && <span style={{ color: 'var(--red)', marginRight: 8 }}>{counts.red} red</span>}
        </span>
      </h3>
      <div>
        {checks.map(c => <CheckRow key={c.id} check={c} />)}
      </div>
    </section>
  );
}

export default function Readiness({ refresh }) {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [countdown, setCountdown] = useState(REFRESH_MS / 1000);
  const [disabled, setDisabled] = useState(false);
  const timerRef = useRef(null);

  const load = useCallback(async ({ fresh = false } = {}) => {
    setLoading(true);
    try {
      const data = await api.getReadiness({ fresh });
      setReport(data);
      setError(null);
      setDisabled(false);
    } catch (e) {
      if (e.message === 'AUTH') { refresh(); return; }
      if (e.message === 'DISABLED') { setDisabled(true); setError(null); }
      else setError(e.message);
    } finally {
      setLoading(false);
      setCountdown(REFRESH_MS / 1000);
    }
  }, [refresh]);

  useEffect(() => { load(); }, [load]);

  // Auto-refresh every 60s with 1s countdown tick.
  useEffect(() => {
    if (disabled) return;
    timerRef.current = setInterval(() => {
      setCountdown(prev => {
        if (prev <= 1) { load(); return REFRESH_MS / 1000; }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timerRef.current);
  }, [load, disabled]);

  const grouped = useMemo(() => {
    if (!report?.checks) return {};
    const g = {};
    for (const c of report.checks) {
      if (!g[c.category]) g[c.category] = [];
      g[c.category].push(c);
    }
    return g;
  }, [report]);

  if (disabled) {
    return (
      <div style={{ padding: 32, textAlign: 'center', color: 'var(--text-secondary)' }}>
        <div style={{ fontSize: 14, marginBottom: 8 }}>Readiness endpoint disabled</div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
          Set <span className="mono">WALACOR_READINESS_ENABLED=true</span> on the gateway to enable.
        </div>
      </div>
    );
  }

  if (loading && !report) return <div style={{ padding: 24, color: 'var(--text-muted)' }}>Loading readiness…</div>;
  if (error) return <div style={{ padding: 24, color: 'var(--red)' }}>Error: {error}</div>;
  if (!report) return null;

  const rollup = report.status || 'degraded';
  const summary = report.summary || { green: 0, amber: 0, red: 0, total: 0 };

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      {/* Headline + controls */}
      <section
        className="status-section"
        style={{
          padding: 20,
          border: '1px solid var(--border)',
          borderLeft: `4px solid ${ROLLUP_COLOR[rollup]}`,
          backgroundColor: 'var(--bg-surface)',
          display: 'flex',
          alignItems: 'center',
          gap: 24,
        }}
      >
        <div>
          <div
            style={{
              fontSize: 28,
              fontWeight: 700,
              letterSpacing: 1,
              color: ROLLUP_COLOR[rollup],
              textTransform: 'uppercase',
            }}
          >
            {rollup}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4 }}>
            Gateway: <span className="mono">{report.gateway_id}</span>
            {report.cache_age_s > 0 && <span> · cached {report.cache_age_s.toFixed(1)}s ago</span>}
          </div>
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 14 }}>
            <span style={{ color: 'var(--green)', marginRight: 14 }}>● {summary.green} green</span>
            <span style={{ color: 'var(--amber)', marginRight: 14 }}>● {summary.amber} amber</span>
            <span style={{ color: 'var(--red)', marginRight: 14 }}>● {summary.red} red</span>
            <span style={{ color: 'var(--text-muted)' }}>of {summary.total}</span>
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
            Next auto-refresh in {countdown}s
          </div>
        </div>
        <button
          className="btn-primary"
          onClick={() => load({ fresh: true })}
          disabled={loading}
        >
          {loading ? 'Rechecking…' : 'Recheck'}
        </button>
      </section>

      {/* Category groups */}
      {CATEGORY_ORDER.filter(cat => grouped[cat]).map(cat => (
        <CategoryGroup key={cat} category={cat} checks={grouped[cat]} />
      ))}
    </div>
  );
}
