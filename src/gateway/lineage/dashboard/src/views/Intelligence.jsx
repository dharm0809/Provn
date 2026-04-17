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
  const [promoteTarget, setPromoteTarget] = useState(null);
  const [rejectTarget, setRejectTarget] = useState(null);

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
                        <button className="btn-primary btn-sm" onClick={() => setPromoteTarget(c)}>
                          Promote
                        </button>
                        <button className="btn-danger btn-sm" onClick={() => setRejectTarget(c)}>
                          Reject
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
      {promoteTarget && (
        <PromoteModal
          candidate={promoteTarget}
          onClose={() => setPromoteTarget(null)}
          onSuccess={async () => { setPromoteTarget(null); await load(); }}
          onAuth={refresh}
        />
      )}
      {rejectTarget && (
        <RejectModal
          candidate={rejectTarget}
          onClose={() => setRejectTarget(null)}
          onSuccess={async () => { setRejectTarget(null); await load(); }}
          onAuth={refresh}
        />
      )}
    </div>
  );
}

// ─── Promote / Reject Modals ────────────────────────────────────

function MetricsTable({ metrics, shadow }) {
  const m = metrics || {};
  const rows = [
    ['Sample count', m.sample_count ?? '-'],
    ['Labeled samples', m.labeled_count ?? '-'],
    ['Candidate accuracy', fmtPct(m.candidate_accuracy)],
    ['Production accuracy', fmtPct(m.production_accuracy)],
    ['Δ accuracy', fmtDelta(m.candidate_accuracy, m.production_accuracy)],
    ['Disagreement rate', fmtPct(m.disagreement_rate)],
    ['Candidate error rate', fmtPct(m.candidate_error_rate)],
    ['McNemar p-value', m.mcnemar_p_value != null ? Number(m.mcnemar_p_value).toFixed(4) : '-'],
  ];
  return (
    <div className="modal-metrics">
      <div className="modal-metrics-head">
        <span>Shadow validation</span>
        <GateBadge shadow={shadow} />
      </div>
      <div className="modal-metrics-grid">
        {rows.map(([label, value]) => (
          <div key={label} className="modal-metrics-row">
            <span className="modal-metrics-label">{label}</span>
            <span className="modal-metrics-value mono">{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function PromoteModal({ candidate, onClose, onSuccess, onAuth }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const c = candidate;
  const m = c.shadow_validation?.metrics || {};
  const gateFailed = c.shadow_validation?.passed === false;
  const noShadow = !c.shadow_validation?.completed;

  const submit = async () => {
    setBusy(true); setError('');
    try {
      await api.promoteCandidate(c.model_name, c.version);
      await onSuccess();
    } catch (e) {
      if (e.message === 'AUTH') { onAuth(); return; }
      setError(e.message || 'promote failed');
      setBusy(false);
    }
  };

  return (
    <div className="confirm-overlay" onClick={busy ? undefined : onClose}>
      <div className="confirm-dialog confirm-dialog-wide" onClick={e => e.stopPropagation()}>
        <h3>Promote Candidate</h3>
        <div className="confirm-item">
          {c.model_name} <span style={{ color: 'var(--text-muted)' }}>·</span>{' '}
          <span className="mono" style={{ fontSize: 12 }}>{c.version}</span>
        </div>
        <p>
          This will replace the current production model. The previous version will be archived
          and a `model_promoted` event will be written to the audit chain.
        </p>

        <MetricsTable metrics={m} shadow={c.shadow_validation} />

        {(gateFailed || noShadow) && (
          <div className="modal-warn">
            {noShadow
              ? '⚠ This candidate has not completed shadow validation. Promote at your own risk.'
              : '⚠ This candidate FAILED its automated promotion gate. Manual override only.'}
          </div>
        )}

        <div className="modal-approver">
          Approver identity is taken from your authenticated session
          (X-User-Id / JWT subject) and recorded on the audit event.
        </div>

        {error && <div className="modal-error">{error}</div>}

        <div className="confirm-actions">
          <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn-primary" onClick={submit} disabled={busy}>
            {busy ? 'Promoting…' : 'Confirm Promote'}
          </button>
        </div>
      </div>
    </div>
  );
}

function RejectModal({ candidate, onClose, onSuccess, onAuth }) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const c = candidate;

  const submit = async () => {
    setBusy(true); setError('');
    try {
      await api.rejectCandidate(c.model_name, c.version, reason.trim());
      await onSuccess();
    } catch (e) {
      if (e.message === 'AUTH') { onAuth(); return; }
      setError(e.message || 'reject failed');
      setBusy(false);
    }
  };

  return (
    <div className="confirm-overlay" onClick={busy ? undefined : onClose}>
      <div className="confirm-dialog" onClick={e => e.stopPropagation()}>
        <h3>Reject Candidate</h3>
        <div className="confirm-item">
          {c.model_name} <span style={{ color: 'var(--text-muted)' }}>·</span>{' '}
          <span className="mono" style={{ fontSize: 12 }}>{c.version}</span>
        </div>
        <p>
          Moves this candidate's `.onnx` to <span className="mono">archive/failed/</span> and
          emits a `model_rejected` event. Production is unaffected.
        </p>
        <div className="form-group" style={{ marginTop: 12 }}>
          <label className="form-label">Reason (optional — defaults to "manual_rejection")</label>
          <input
            className="form-input"
            placeholder="e.g. accuracy regression on web_search class"
            value={reason}
            onChange={e => setReason(e.target.value)}
            autoFocus
            disabled={busy}
            onKeyDown={e => e.key === 'Enter' && !busy && submit()}
          />
        </div>
        {error && <div className="modal-error">{error}</div>}
        <div className="confirm-actions">
          <button className="btn" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn-danger" onClick={submit} disabled={busy}>
            {busy ? 'Rejecting…' : 'Confirm Reject'}
          </button>
        </div>
      </div>
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
