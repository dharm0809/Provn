import { useState, useEffect, useCallback } from 'react';
import * as api from '../api';
import { formatNumber, formatTime, timeAgo } from '../utils';

const SUB_TABS = [
  { key: 'production', label: 'Production' },
  { key: 'candidates', label: 'Candidates' },
  { key: 'history',    label: 'Promotion History' },
  { key: 'verdicts',   label: 'Verdict Inspector' },
];

function Placeholder({ title, hint }) {
  return (
    <div className="card">
      <div className="card-head">
        <span className="card-title">{title}</span>
      </div>
      <div className="empty-state"><p>{hint}</p></div>
    </div>
  );
}

// ─── Production Models ──────────────────────────────────────────

function ProductionView({ refresh }) {
  const [models, setModels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true); setError('');
    try {
      const data = await api.getIntelligenceModels();
      setModels(data.models || []);
    } catch (e) {
      if (e.message === 'AUTH') { refresh(); return; }
      setError(e.message || 'failed to load production models');
    } finally {
      setLoading(false);
    }
  }, [refresh]);

  useEffect(() => { load(); }, [load]);

  if (loading) return <div className="skeleton-block" style={{ height: 180 }} />;

  return (
    <div className="card">
      <div className="card-head">
        <span className="card-title">Production Models ({models.length})</span>
        <button className="btn btn-sm" onClick={load}>Refresh</button>
      </div>
      {error && <div className="empty-state"><p style={{ color: 'var(--red)' }}>{error}</p></div>}
      {!error && models.length === 0 ? (
        <div className="empty-state">
          <p>No production models loaded. Models seed on first inference; check intelligence layer config.</p>
        </div>
      ) : !error && (
        <div className="table-wrap">
          <table>
            <thead><tr>
              <th>Model</th>
              <th>Active Version</th>
              <th>Generation</th>
              <th>Approver</th>
              <th>Last Promoted</th>
              <th style={{ textAlign: 'right' }}>Size</th>
            </tr></thead>
            <tbody>
              {models.map(m => {
                const lp = m.last_promotion;
                const versionCell = lp?.candidate_version
                  ? <span className="mono">{lp.candidate_version}</span>
                  : <span className="badge badge-muted">baseline</span>;
                return (
                  <tr key={m.model_name}>
                    <td className="id">{m.model_name}</td>
                    <td>{versionCell}</td>
                    <td className="mono">{m.generation ?? 0}</td>
                    <td>{lp?.approver
                      ? <span className="badge badge-muted">{lp.approver}</span>
                      : <span style={{ color: 'var(--text-muted)' }}>-</span>}</td>
                    <td title={lp?.timestamp ? formatTime(lp.timestamp) : ''}>
                      {lp?.timestamp ? timeAgo(lp.timestamp) : '-'}
                    </td>
                    <td className="mono" style={{ textAlign: 'right' }}>
                      {m.size_bytes ? formatNumber(m.size_bytes) + 'B' : '-'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div style={{ padding: '8px 16px 14px', fontSize: 11, color: 'var(--text-muted)' }}>
        Prediction count and trailing accuracy require a verdict-log aggregation; coming after API enhancement.
      </div>
    </div>
  );
}

// ─── Candidates ─────────────────────────────────────────────────

function fmtPct(x) {
  if (x == null || Number.isNaN(x)) return '-';
  return (Number(x) * 100).toFixed(1) + '%';
}

function fmtDelta(candidate, production) {
  if (candidate == null || production == null) return '-';
  const d = Number(candidate) - Number(production);
  if (Number.isNaN(d)) return '-';
  const sign = d >= 0 ? '+' : '';
  return sign + (d * 100).toFixed(1) + 'pp';
}

function GateBadge({ shadow }) {
  if (!shadow?.completed) {
    return <span className="badge badge-muted">no shadow</span>;
  }
  if (shadow.passed === true) return <span className="badge badge-pass">gate passed</span>;
  if (shadow.passed === false) return <span className="badge badge-fail">gate failed</span>;
  return <span className="badge badge-warn">unknown</span>;
}

function CandidatesView({ refresh }) {
  const [candidates, setCandidates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(null);

  const load = useCallback(async () => {
    setLoading(true); setError('');
    try {
      const data = await api.getIntelligenceCandidates();
      setCandidates(data.candidates || []);
    } catch (e) {
      if (e.message === 'AUTH') { refresh(); return; }
      setError(e.message || 'failed to load candidates');
    } finally {
      setLoading(false);
    }
  }, [refresh]);

  useEffect(() => { load(); }, [load]);

  // Task 32 will replace these window.confirm calls with rich modals
  // (metrics preview + approver identity confirmation).
  const onPromote = async (c) => {
    if (!window.confirm(`Promote ${c.model_name} ${c.version}?\n\nThis will replace the current production model. The previous version will be archived.`)) return;
    setBusy(`promote:${c.model_name}:${c.version}`);
    try {
      await api.promoteCandidate(c.model_name, c.version);
      await load();
    } catch (e) {
      if (e.message === 'AUTH') { refresh(); return; }
      window.alert(`Promote failed: ${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  const onReject = async (c) => {
    const reason = window.prompt(`Reject ${c.model_name} ${c.version}? Optional reason:`, '');
    if (reason === null) return;
    setBusy(`reject:${c.model_name}:${c.version}`);
    try {
      await api.rejectCandidate(c.model_name, c.version, reason || '');
      await load();
    } catch (e) {
      if (e.message === 'AUTH') { refresh(); return; }
      window.alert(`Reject failed: ${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  if (loading) return <div className="skeleton-block" style={{ height: 180 }} />;

  return (
    <div className="card">
      <div className="card-head">
        <span className="card-title">Candidates ({candidates.length})</span>
        <button className="btn btn-sm" onClick={load}>Refresh</button>
      </div>
      {error && <div className="empty-state"><p style={{ color: 'var(--red)' }}>{error}</p></div>}
      {!error && candidates.length === 0 ? (
        <div className="empty-state"><p>No candidates yet. Trigger Force Retrain or wait for the distillation worker.</p></div>
      ) : !error && (
        <div className="table-wrap">
          <table>
            <thead><tr>
              <th>Model</th>
              <th>Version</th>
              <th>Shadow</th>
              <th>Samples</th>
              <th>Cand. Acc</th>
              <th>Prod. Acc</th>
              <th>Δ Acc</th>
              <th>Disagreement</th>
              <th>p-value</th>
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr></thead>
            <tbody>
              {candidates.map(c => {
                const m = c.shadow_validation?.metrics || {};
                const key = `${c.model_name}:${c.version}`;
                const promoteBusy = busy === `promote:${c.model_name}:${c.version}`;
                const rejectBusy = busy === `reject:${c.model_name}:${c.version}`;
                return (
                  <tr key={key}>
                    <td className="id">{c.model_name}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{c.version}</td>
                    <td>
                      <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                        {c.active_shadow && <span className="badge badge-pass" style={{ fontSize: 10 }}>active</span>}
                        <GateBadge shadow={c.shadow_validation} />
                      </div>
                    </td>
                    <td className="mono">{m.sample_count ?? '-'}</td>
                    <td className="mono">{fmtPct(m.candidate_accuracy)}</td>
                    <td className="mono">{fmtPct(m.production_accuracy)}</td>
                    <td className="mono">{fmtDelta(m.candidate_accuracy, m.production_accuracy)}</td>
                    <td className="mono">{fmtPct(m.disagreement_rate)}</td>
                    <td className="mono">{m.mcnemar_p_value != null ? Number(m.mcnemar_p_value).toFixed(3) : '-'}</td>
                    <td>
                      <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                        <button className="btn-primary btn-sm" disabled={promoteBusy || rejectBusy}
                          onClick={() => onPromote(c)}>
                          {promoteBusy ? '…' : 'Promote'}
                        </button>
                        <button className="btn-danger btn-sm" disabled={promoteBusy || rejectBusy}
                          onClick={() => onReject(c)}>
                          {rejectBusy ? '…' : 'Reject'}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Main view ──────────────────────────────────────────────────

export default function Intelligence({ refresh }) {
  const [sub, setSub] = useState('production');

  return (
    <div className="fade-child">
      <div className="control-subnav">
        {SUB_TABS.map(t => (
          <button
            key={t.key}
            className={`control-subtab${sub === t.key ? ' active' : ''}`}
            onClick={() => setSub(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {sub === 'production' && <ProductionView refresh={refresh} />}
      {sub === 'candidates' && <CandidatesView refresh={refresh} />}
      {sub === 'history' && (
        <Placeholder
          title="Promotion History"
          hint="Past promotions, rejections, and rollback controls will appear here."
        />
      )}
      {sub === 'verdicts' && (
        <Placeholder
          title="Verdict Inspector"
          hint="Per-model divergence breakdown, verdict log samples, and force-retrain controls will appear here."
        />
      )}
    </div>
  );
}
