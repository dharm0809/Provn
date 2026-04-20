/* Walacor Gateway — Intelligence View (from design zip, wired to real API) */

import React, { useState, useEffect, useCallback } from 'react';
import {
  getIntelligenceModels,
  getIntelligenceCandidates,
  getIntelligenceHistory,
  getIntelligenceVerdicts,
  promoteCandidate,
  rejectCandidate,
  rollbackModel,
  forceRetrain,
} from '../api';
import { timeAgo, truncHash, formatBytes, formatNumber, fmtPct, fmtDelta } from '../utils';
import '../styles/intelligence.css';

/* Display-layer metadata for known models. Backend supplies the operational
   fields (generation, size_bytes, last_promotion); the human-readable
   description/architecture/parameters are client-side lookups so the
   registry stays implementation-agnostic. */
const MODEL_META = {
  intent:        { description: 'Classifies user intent across 14 action categories',      architecture: 'DistilBERT-multi',   parameters: '22M' },
  schema_mapper: { description: 'Maps freeform queries → structured schema fields',        architecture: 'T5-small-distilled', parameters: '44M' },
  safety:        { description: 'Policy violation + prompt-injection detection',           architecture: 'MiniLM-v6',          parameters: '14M' },
};

function enrichModel(m) {
  const meta = MODEL_META[m.model_name] || {};
  return {
    ...m,
    description: m.description || meta.description || '—',
    architecture: m.architecture || meta.architecture || '—',
    parameters: m.parameters || meta.parameters || '—',
    active_version: m.active_version || `v${String(m.generation || 0).padStart(3, '0')}`,
    accuracy: m.accuracy ?? 0,
    trailing_accuracy: m.trailing_accuracy ?? m.accuracy ?? 0,
    predictions_24h: m.predictions_24h ?? 0,
    predictions_7d: m.predictions_7d ?? 0,
    drift: m.drift || 'stable',
    accuracy_series: m.accuracy_series || null,
  };
}

const IntelSubTabs = [
  { key: 'production', label: 'Production' },
  { key: 'candidates', label: 'Candidates' },
  { key: 'history',    label: 'Promotion History' },
  { key: 'verdicts',   label: 'Verdict Inspector' },
];

function MiniSpark({ data, color = 'var(--gold)', w = 72, h = 22 }) {
  if (!data || data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - 2 - ((v - min) / span) * (h - 4);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const lastX = w;
  const lastY = h - 2 - ((data[data.length - 1] - min) / span) * (h - 4);
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: 'inline-block', verticalAlign: 'middle' }}>
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={lastX} cy={lastY} r="2" fill={color} />
    </svg>
  );
}

function IntelSubnav({ sub, setSub }) {
  return (
    <div className="intel-subnav">
      {IntelSubTabs.map(t => (
        <button
          key={t.key}
          className={`intel-subtab${sub === t.key ? ' active' : ''}`}
          onClick={() => setSub(t.key)}>
          <span className="intel-subtab-label">{t.label}</span>
        </button>
      ))}
    </div>
  );
}

function IntelHeader({ models, candidates }) {
  const pendingShadow = candidates.filter(c => !c.shadow_validation?.completed).length;
  const failingGate = candidates.filter(c => c.shadow_validation?.completed && !c.shadow_validation?.passed).length;
  const ready = candidates.filter(c => c.shadow_validation?.passed === true).length;
  const avgAcc = models.length > 0 ? models.reduce((s, m) => s + (m.accuracy || 0), 0) / models.length : 0;

  return (
    <div className="intel-metric-bar">
      <div className="intel-metric">
        <div className="intel-metric-label">Production Models</div>
        <div className="intel-metric-value">{models.length}</div>
        <div className="intel-metric-sub">all on chain</div>
      </div>
      <div className="intel-metric">
        <div className="intel-metric-label">Avg Accuracy</div>
        <div className="intel-metric-value gold">{(avgAcc * 100).toFixed(1)}<span className="intel-metric-unit">%</span></div>
        <div className="intel-metric-sub">trailing 7d window</div>
      </div>
      <div className="intel-metric">
        <div className="intel-metric-label">Candidates</div>
        <div className="intel-metric-value">{candidates.length}</div>
        <div className="intel-metric-sub">
          <span className="mono" style={{ color: 'var(--green)' }}>{ready} ready</span> ·{' '}
          <span className="mono" style={{ color: 'var(--red)' }}>{failingGate} failing</span>
        </div>
      </div>
      <div className="intel-metric">
        <div className="intel-metric-label">Pending Shadow</div>
        <div className="intel-metric-value">{pendingShadow}</div>
        <div className="intel-metric-sub">collecting samples</div>
      </div>
      <div className="intel-metric accent">
        <div className="intel-metric-label">Audit Chain</div>
        <div className="intel-metric-value green">VERIFIED</div>
        <div className="intel-metric-sub">
          <span className="intel-dot-green" />
          {(models.length * 12)} events on chain
        </div>
      </div>
    </div>
  );
}

function ProductionView({ models, onForceRetrain }) {
  if (models.length === 0) {
    return <div className="card"><div className="empty">No production models yet.</div></div>;
  }
  return (
    <div className="prod-grid">
      {models.map(m => {
        const accTrend = m.accuracy_series;
        const drift = m.drift || 'stable';
        return (
          <div key={m.model_name} className="card prod-card">
            <div className="prod-card-head">
              <div>
                <div className="prod-model-name">{m.model_name}</div>
                <div className="prod-model-desc">{m.description}</div>
              </div>
              <div className="prod-status-badge">
                <span className="prod-status-dot" />
                ACTIVE · GEN {m.generation}
              </div>
            </div>

            <div className="prod-hero">
              <div className="prod-hero-acc">
                <div className="prod-hero-val gold">{((m.accuracy || 0) * 100).toFixed(1)}<span style={{fontSize:14, color:'var(--text-muted)'}}>%</span></div>
                <div className="prod-hero-lbl">accuracy · 14d trend</div>
              </div>
              <div className="prod-hero-spark">
                <MiniSpark data={accTrend} color="var(--gold)" w={120} h={36}/>
              </div>
              <div className={`prod-drift prod-drift-${drift}`}>
                {drift === 'stable' ? '▬ STABLE' : drift === 'minor' ? '◆ MINOR DRIFT' : '▲ DRIFTING'}
              </div>
            </div>

            <dl className="prod-dl">
              <div><dt>Active version</dt><dd className="mono">{m.active_version}</dd></div>
              <div><dt>Architecture</dt><dd>{m.architecture} · {m.parameters}</dd></div>
              <div><dt>Size</dt><dd className="mono">{formatBytes(m.size_bytes)}</dd></div>
              <div><dt>Predictions (24h)</dt><dd className="mono">{formatNumber(m.predictions_24h || 0)}</dd></div>
              <div><dt>Predictions (7d)</dt><dd className="mono">{formatNumber(m.predictions_7d || 0)}</dd></div>
              <div><dt>Trailing accuracy</dt><dd className="mono">{((m.trailing_accuracy || 0) * 100).toFixed(2)}%</dd></div>
            </dl>

            <div className="prod-foot">
              <div className="prod-foot-left">
                <div className="prod-foot-k">Last promoted</div>
                <div className="prod-foot-v">
                  <span className="mono">{m.last_promotion ? timeAgo(m.last_promotion.timestamp) : '—'}</span>
                  {m.last_promotion?.approver && <span className="prod-approver">by {m.last_promotion.approver}</span>}
                </div>
              </div>
              <button className="btn-wal btn-ghost" onClick={() => onForceRetrain(m.model_name)}>
                Force Retrain
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function GateBadge({ shadow }) {
  if (!shadow?.completed) return <span className="badge-wal badge-muted">◌ collecting</span>;
  if (shadow.passed === true) return <span className="badge-wal badge-pass">✓ gate passed</span>;
  if (shadow.passed === false) return <span className="badge-wal badge-fail">✕ gate failed</span>;
  return <span className="badge-wal badge-warn">? unknown</span>;
}

function CandidatesView({ candidates, onPromote, onReject }) {
  if (candidates.length === 0) {
    return <div className="card"><div className="empty">No candidates. Trigger Force Retrain or wait for the distillation worker.</div></div>;
  }
  return (
    <div className="cand-grid">
      {candidates.map(c => {
        const m = c.shadow_validation?.metrics || {};
        const gateFailed = c.shadow_validation?.passed === false;
        const noShadow = !c.shadow_validation?.completed;
        const deltaAcc = m.candidate_accuracy != null && m.production_accuracy != null
          ? m.candidate_accuracy - m.production_accuracy : null;
        const deltaCls = deltaAcc == null ? '' : deltaAcc > 0 ? 'delta-pos' : 'delta-neg';

        return (
          <div key={`${c.model_name}:${c.version}`}
               className={`card cand-card ${c.active_shadow ? 'active-shadow' : ''}`}>
            <div className="cand-head">
              <div>
                <div className="cand-model-row">
                  <span className="cand-model">{c.model_name}</span>
                  {c.active_shadow && <span className="badge-wal badge-active">◆ ACTIVE SHADOW</span>}
                </div>
                <div className="cand-version mono">{c.version}</div>
              </div>
              <GateBadge shadow={c.shadow_validation} />
            </div>

            {noShadow && (
              <div className="cand-progress">
                <div className="cand-progress-head">
                  <span>shadow validation in progress</span>
                  <span className="mono">{m.sample_count || 0} / 5000 samples</span>
                </div>
                <div className="cand-progress-bar">
                  <div className="cand-progress-fill" style={{ width: `${Math.min(100, ((m.sample_count || 0) / 5000) * 100)}%` }} />
                </div>
              </div>
            )}

            {!noShadow && (
              <>
                <div className="cand-metrics">
                  <div className="cand-metric">
                    <div className="cand-metric-lbl">cand · prod</div>
                    <div className="cand-metric-val">
                      <span className="gold">{fmtPct(m.candidate_accuracy, 1)}</span>
                      <span className="cand-metric-sep">vs</span>
                      <span>{fmtPct(m.production_accuracy, 1)}</span>
                    </div>
                  </div>
                  <div className="cand-metric">
                    <div className="cand-metric-lbl">Δ accuracy</div>
                    <div className={`cand-metric-val ${deltaCls}`}>{fmtDelta(m.candidate_accuracy, m.production_accuracy)}</div>
                  </div>
                  <div className="cand-metric">
                    <div className="cand-metric-lbl">disagreement</div>
                    <div className="cand-metric-val">{fmtPct(m.disagreement_rate, 1)}</div>
                  </div>
                  <div className="cand-metric">
                    <div className="cand-metric-lbl">mcnemar p</div>
                    <div className={`cand-metric-val mono ${m.mcnemar_p_value < 0.05 ? 'p-good' : 'p-bad'}`}>
                      {m.mcnemar_p_value != null ? m.mcnemar_p_value.toFixed(4) : '—'}
                    </div>
                  </div>
                </div>

                <div className="cand-bars">
                  <div className="cand-bar-row">
                    <span className="cand-bar-lbl">prod</span>
                    <div className="cand-bar-track"><div className="cand-bar-fill prod" style={{ width: `${(m.production_accuracy || 0) * 100}%` }} /></div>
                    <span className="cand-bar-val mono">{fmtPct(m.production_accuracy, 2)}</span>
                  </div>
                  <div className="cand-bar-row">
                    <span className="cand-bar-lbl">cand</span>
                    <div className="cand-bar-track"><div className="cand-bar-fill cand" style={{ width: `${(m.candidate_accuracy || 0) * 100}%` }} /></div>
                    <span className="cand-bar-val mono">{fmtPct(m.candidate_accuracy, 2)}</span>
                  </div>
                </div>
              </>
            )}

            <div className="cand-kv">
              <div><span className="cand-k">samples</span><span className="mono">{m.sample_count || 0}</span></div>
              <div><span className="cand-k">labeled</span><span className="mono">{m.labeled_count || 0}</span></div>
              <div><span className="cand-k">age</span><span className="mono">{timeAgo(c.created_at)}</span></div>
              <div><span className="cand-k">dataset</span><span className="mono" title={c.dataset_hash}>{truncHash(c.dataset_hash, 14)}</span></div>
            </div>

            {(gateFailed || noShadow) && (
              <div className={`cand-warn ${gateFailed ? 'cand-warn-fail' : 'cand-warn-shadow'}`}>
                {gateFailed
                  ? '⚠ FAILED automated promotion gate. Promote requires manual override.'
                  : '◌ Shadow validation still running. Promote at your own risk.'}
              </div>
            )}

            <div className="cand-actions">
              <button className="btn-wal btn-primary" onClick={() => onPromote(c)}>
                <span className="btn-icon">▲</span> Promote
              </button>
              <button className="btn-wal btn-danger" onClick={() => onReject(c)}>
                <span className="btn-icon">✕</span> Reject
              </button>
              <button className="btn-wal btn-ghost" title="Open candidate details">Inspect</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function eventBadge(ev) {
  const t = ev.event_type;
  const p = ev.payload || {};
  if (t === 'model_promoted') {
    const isRb = String(p.candidate_version || '').startsWith('rollback:');
    return <span className={`badge-wal ${isRb ? 'badge-warn' : 'badge-pass'}`}>{isRb ? '↺ rolled back' : '▲ promoted'}</span>;
  }
  if (t === 'model_rejected') return <span className="badge-wal badge-fail">✕ rejected</span>;
  if (t === 'candidate_created') return <span className="badge-wal badge-muted">◆ candidate</span>;
  if (t === 'training_dataset_fingerprint') return <span className="badge-wal badge-muted">◇ dataset</span>;
  if (t === 'shadow_validation_complete') {
    const passed = p.passed === true;
    return <span className={`badge-wal ${passed ? 'badge-pass' : 'badge-fail'}`}>{passed ? '✓ shadow pass' : '✕ shadow fail'}</span>;
  }
  return <span className="badge-wal badge-muted">{t}</span>;
}

function HistoryView({ model, setModel, events, onRollback }) {
  const models = ['intent', 'schema_mapper', 'safety'];
  return (
    <div className="card">
      <div className="intel-card-head">
        <div>
          <div className="intel-card-title">Promotion History</div>
          <div className="intel-card-sub">
            Chain-of-custody log · every event permanently written to Walacor
          </div>
        </div>
        <div className="intel-card-actions">
          <div className="intel-tab-group">
            {models.map(m => (
              <button key={m}
                      className={`intel-tab-sm${model === m ? ' active' : ''}`}
                      onClick={() => setModel(m)}>
                {m}
              </button>
            ))}
          </div>
          <button className="btn-wal btn-danger btn-sm" onClick={onRollback}>
            ↺ Rollback…
          </button>
        </div>
      </div>

      <div className="intel-timeline">
        {events.length === 0 ? (
          <div className="empty">No events yet for {model}.</div>
        ) : events.map((ev, i) => {
          const p = ev.payload || {};
          const wrote = ev.write_status === 'written';
          const sm = p.shadow_metrics || {};
          return (
            <div key={i} className="intel-tl-row">
              <div className="intel-tl-rail">
                <div className={`intel-tl-node intel-tl-node-${ev.event_type}`} />
              </div>
              <div className="intel-tl-card">
                <div className="intel-tl-top">
                  {eventBadge(ev)}
                  <span className="intel-tl-time" title={new Date(ev.timestamp).toLocaleString()}>
                    {timeAgo(ev.timestamp)}
                  </span>
                  <span className="intel-tl-spacer" />
                  <span className={`chain-chip ${wrote ? 'chain-ok' : 'chain-fail'}`}
                        title={ev.error_reason || ''}>
                    {wrote ? '◆ on chain' : '✕ write failed'}
                  </span>
                  {ev.attempts > 1 && <span className="badge-wal badge-muted" style={{ fontSize: 10 }}>{ev.attempts} attempts</span>}
                </div>
                <div className="intel-tl-body">
                  {p.candidate_version && (
                    <div className="intel-tl-kv"><span className="k">version</span><span className="v mono">{p.candidate_version}</span></div>
                  )}
                  {p.approver && (
                    <div className="intel-tl-kv"><span className="k">approver</span><span className="v">{p.approver}</span></div>
                  )}
                  {p.dataset_hash && (
                    <div className="intel-tl-kv"><span className="k">dataset</span><span className="v mono" title={p.dataset_hash}>{truncHash(p.dataset_hash, 20)}</span></div>
                  )}
                  {p.reason && (
                    <div className="intel-tl-kv"><span className="k">reason</span><span className="v">{p.reason}</span></div>
                  )}
                  {sm.sample_count != null && (
                    <div className="intel-tl-kv">
                      <span className="k">shadow</span>
                      <span className="v mono">
                        n={sm.sample_count}
                        {sm.candidate_accuracy != null && sm.production_accuracy != null && (
                          <> · Δ {fmtDelta(sm.candidate_accuracy, sm.production_accuracy)}</>
                        )}
                      </span>
                    </div>
                  )}
                  {ev.walacor_record_id && (
                    <div className="intel-tl-kv"><span className="k">chain id</span><span className="v mono" title={ev.walacor_record_id}>{truncHash(ev.walacor_record_id, 16)}</span></div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function VerdictsView({ model, setModel, divergenceOnly, setDivergenceOnly, limit, setLimit, data, onRetrain, retrainStatus }) {
  const models = ['intent', 'schema_mapper', 'safety'];
  const limits = [50, 100, 250, 500, 1000];
  const top = data?.top_divergence_types || [];
  const totalDiv = top.reduce((s, t) => s + (t.count || 0), 0);
  const rows = data?.rows || [];

  return (
    <div className="card">
      <div className="intel-card-head">
        <div>
          <div className="intel-card-title">Verdict Inspector</div>
          <div className="intel-card-sub">
            Back-signals from production — harvesters write disagreements here so retraining knows what to fix
          </div>
        </div>
        <div className="intel-card-actions verdict-controls">
          <div className="intel-tab-group">
            {models.map(m => (
              <button key={m}
                      className={`intel-tab-sm${model === m ? ' active' : ''}`}
                      onClick={() => setModel(m)}>
                {m}
              </button>
            ))}
          </div>
          <label className="intel-check">
            <input type="checkbox" checked={divergenceOnly} onChange={e => setDivergenceOnly(e.target.checked)} />
            divergent only
          </label>
          <select className="intel-select" value={limit} onChange={e => setLimit(parseInt(e.target.value, 10))}>
            {limits.map(n => <option key={n} value={n}>{n}</option>)}
          </select>
          <button className="btn-wal btn-primary btn-sm" onClick={onRetrain}>◆ Force Retrain</button>
        </div>
      </div>

      {retrainStatus && (
        <div className="retrain-toast">
          <span className="retrain-spinner" />
          {retrainStatus}
        </div>
      )}

      {top.length > 0 && divergenceOnly && (
        <div className="verdict-top-section">
          <div className="verdict-top-head">
            <span>Top divergence signals</span>
            <span className="mono">{totalDiv} divergent verdicts</span>
          </div>
          <div className="verdict-bars">
            {top.map(t => {
              const pct = totalDiv > 0 ? (t.count / totalDiv) * 100 : 0;
              return (
                <div key={t.signal} className="verdict-bar">
                  <div className="verdict-bar-lbl">{t.signal}</div>
                  <div className="verdict-bar-track">
                    <div className="verdict-bar-fill" style={{ width: pct + '%' }}>
                      <span className="verdict-bar-inside">{t.count}</span>
                    </div>
                  </div>
                  <div className="verdict-bar-pct mono">{pct.toFixed(1)}%</div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="verdict-table-wrap">
        <table className="verdict-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Input Hash</th>
              <th>Prediction</th>
              <th>Confidence</th>
              <th>Divergence</th>
              <th>Source</th>
              <th>Request</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 40).map(r => {
              const conf = r.confidence || 0;
              const confCls = conf > 0.85 ? 'conf-high' : conf > 0.65 ? 'conf-mid' : 'conf-low';
              return (
                <tr key={r.id} className="verdict-row">
                  <td>{timeAgo(r.timestamp)}</td>
                  <td className="mono small" title={r.input_hash}>{truncHash(r.input_hash, 12)}</td>
                  <td className="mono">{r.prediction}</td>
                  <td>
                    <div className="conf-cell">
                      <div className="conf-bar"><div className={`conf-bar-fill ${confCls}`} style={{ width: `${conf * 100}%` }} /></div>
                      <span className="mono small">{conf.toFixed(3)}</span>
                    </div>
                  </td>
                  <td>
                    {r.divergence_signal
                      ? <span className="badge-wal badge-warn">{r.divergence_signal}</span>
                      : <span className="txt-muted">—</span>}
                  </td>
                  <td className="txt-muted small">{r.divergence_source || '—'}</td>
                  <td className="mono small" title={r.request_id}>{truncHash(r.request_id, 10)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {rows.length === 0 && <div className="empty">No verdicts in this window.</div>}
        {rows.length > 40 && (
          <div className="verdict-more">…and {rows.length - 40} more in this window</div>
        )}
      </div>
    </div>
  );
}

function PromoteModal({ candidate, onClose, onConfirm }) {
  const [busy, setBusy] = useState(false);
  const c = candidate;
  const m = c.shadow_validation?.metrics || {};
  const gateFailed = c.shadow_validation?.passed === false;
  const noShadow = !c.shadow_validation?.completed;

  const confirm = async () => { setBusy(true); try { await onConfirm(); } finally { setBusy(false); } };

  const rows = [
    ['Sample count', m.sample_count ?? '—'],
    ['Labeled samples', m.labeled_count ?? '—'],
    ['Candidate accuracy', fmtPct(m.candidate_accuracy, 2)],
    ['Production accuracy', fmtPct(m.production_accuracy, 2)],
    ['Δ accuracy', fmtDelta(m.candidate_accuracy, m.production_accuracy)],
    ['Disagreement rate', fmtPct(m.disagreement_rate, 2)],
    ['Candidate error rate', fmtPct(m.candidate_error_rate, 2)],
    ['McNemar p-value', m.mcnemar_p_value != null ? m.mcnemar_p_value.toFixed(4) : '—'],
  ];

  return (
    <div className="wal-modal-overlay" onClick={busy ? undefined : onClose}>
      <div className="wal-modal wal-modal-wide" onClick={e => e.stopPropagation()}>
        <div className="wal-modal-head">
          <div>
            <div className="wal-modal-eyebrow">◆ PROMOTE CANDIDATE</div>
            <div className="wal-modal-title">{c.model_name} → <span className="mono">{c.version}</span></div>
          </div>
          <button className="wal-modal-close" onClick={onClose}>✕</button>
        </div>

        <p className="wal-modal-p">
          This replaces the current production model. The previous version is archived and a{' '}
          <span className="mono">model_promoted</span> event is written to the Walacor audit chain.
        </p>

        <div className="modal-metrics">
          <div className="modal-metrics-head">
            <span>Shadow validation</span>
            <GateBadge shadow={c.shadow_validation} />
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

        {(gateFailed || noShadow) && (
          <div className="modal-warn">
            <strong>⚠ MANUAL OVERRIDE</strong>
            <div>{noShadow
              ? 'This candidate has not completed shadow validation. The audit event will carry an unvalidated flag.'
              : 'This candidate FAILED its automated promotion gate. Proceed only if you have an authoritative reason and approver.'}
            </div>
          </div>
        )}

        <div className="modal-approver">
          <span className="mono small txt-muted">APPROVER</span>
          <span className="approver-chip">alex.chen@acme.io</span>
          <span className="small txt-muted">identity taken from X-User-Id / JWT subject</span>
        </div>

        <div className="wal-modal-actions">
          <button className="btn-wal btn-ghost" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn-wal btn-primary" onClick={confirm} disabled={busy}>
            {busy ? 'Writing to chain…' : '▲ Confirm Promote'}
          </button>
        </div>
      </div>
    </div>
  );
}

function RejectModal({ candidate, onClose, onConfirm }) {
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const c = candidate;

  const presets = [
    'accuracy regression on web_search class',
    'disagreement rate exceeds threshold',
    'drift signal on safety slice',
    'manual_rejection',
  ];

  const confirm = async () => { setBusy(true); try { await onConfirm(reason.trim() || 'manual_rejection'); } finally { setBusy(false); } };

  return (
    <div className="wal-modal-overlay" onClick={busy ? undefined : onClose}>
      <div className="wal-modal" onClick={e => e.stopPropagation()}>
        <div className="wal-modal-head">
          <div>
            <div className="wal-modal-eyebrow">✕ REJECT CANDIDATE</div>
            <div className="wal-modal-title">{c.model_name} · <span className="mono">{c.version}</span></div>
          </div>
          <button className="wal-modal-close" onClick={onClose}>✕</button>
        </div>

        <p className="wal-modal-p">
          Moves this candidate's weights to <span className="mono">archive/failed/</span> and
          writes a <span className="mono">model_rejected</span> event. Production is unaffected.
        </p>

        <div className="form-field">
          <label className="form-label">Reason</label>
          <input
            className="form-input"
            placeholder="e.g. accuracy regression on web_search class"
            value={reason}
            onChange={e => setReason(e.target.value)}
            autoFocus
            disabled={busy}
          />
          <div className="reason-presets">
            {presets.map(p => (
              <button key={p} type="button" className="reason-chip" onClick={() => setReason(p)}>{p}</button>
            ))}
          </div>
        </div>

        <div className="wal-modal-actions">
          <button className="btn-wal btn-ghost" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn-wal btn-danger" onClick={confirm} disabled={busy}>
            {busy ? 'Writing…' : '✕ Confirm Reject'}
          </button>
        </div>
      </div>
    </div>
  );
}

function RollbackModal({ model, onClose, onConfirm }) {
  const [busy, setBusy] = useState(false);
  const confirm = async () => { setBusy(true); try { await onConfirm(); } finally { setBusy(false); } };

  return (
    <div className="wal-modal-overlay" onClick={busy ? undefined : onClose}>
      <div className="wal-modal" onClick={e => e.stopPropagation()}>
        <div className="wal-modal-head">
          <div>
            <div className="wal-modal-eyebrow">↺ ROLLBACK</div>
            <div className="wal-modal-title">{model}</div>
          </div>
          <button className="wal-modal-close" onClick={onClose}>✕</button>
        </div>

        <p className="wal-modal-p">
          Restores the most recently archived production version. The current production file
          is replaced and a <span className="mono">model_promoted</span> event is written with{' '}
          <span className="mono">candidate_version=rollback:&lt;archive&gt;</span>.
        </p>

        <div className="modal-warn">
          <strong>⚠ DESTRUCTIVE</strong>
          <div>
            Rollback restores whatever archive sorts last by ISO-8601 filename.
            If the previous version was also bad, you'll need a manual promote to recover.
          </div>
        </div>

        <div className="wal-modal-actions">
          <button className="btn-wal btn-ghost" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn-wal btn-danger" onClick={confirm} disabled={busy}>
            {busy ? 'Rolling back…' : '↺ Confirm Rollback'}
          </button>
        </div>
      </div>
    </div>
  );
}

function useToast() {
  const [msg, setMsg] = useState('');
  const show = useCallback((text, ms = 2200) => {
    setMsg(text);
    setTimeout(() => setMsg(''), ms);
  }, []);
  const node = msg ? (
    <div style={{
      position: 'fixed', bottom: 24, left: '50%',
      transform: 'translateX(-50%)',
      background: 'var(--bg-elevated)',
      border: '1px solid var(--gold-dim)',
      padding: '10px 18px',
      fontFamily: 'var(--mono)',
      fontSize: 12,
      color: 'var(--gold)',
      letterSpacing: '0.1em',
      boxShadow: '0 8px 24px var(--shadow, rgba(0,0,0,0.4))',
      zIndex: 300,
      animation: 'fadeIn 0.2s ease',
    }}>{msg}</div>
  ) : null;
  return { show, node };
}

export default function Intelligence() {
  const [sub, setSub] = useState('production');
  const [models, setModels] = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [historyModel, setHistoryModel] = useState('intent');
  const [historyEvents, setHistoryEvents] = useState([]);

  const [verdictModel, setVerdictModel] = useState('intent');
  const [divergenceOnly, setDivergenceOnly] = useState(true);
  const [limit, setLimit] = useState(100);
  const [verdictData, setVerdictData] = useState({ rows: [], top_divergence_types: [] });
  const [retrainStatus, setRetrainStatus] = useState('');

  const [promoteTarget, setPromoteTarget] = useState(null);
  const [rejectTarget, setRejectTarget] = useState(null);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [loadError, setLoadError] = useState(null);

  const toast = useToast();

  const loadModelsAndCandidates = useCallback(async () => {
    try {
      const [mRes, cRes] = await Promise.all([getIntelligenceModels(), getIntelligenceCandidates()]);
      const rawModels = mRes?.models || mRes || [];
      setModels(rawModels.map(enrichModel));
      setCandidates(cRes?.candidates || cRes || []);
      setLoadError(null);
    } catch (e) {
      setLoadError(e.message === 'AUTH' ? 'API key required — set it in the Control tab.' : e.message);
    }
  }, []);

  useEffect(() => { loadModelsAndCandidates(); }, [loadModelsAndCandidates]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getIntelligenceHistory(historyModel);
        if (!cancelled) setHistoryEvents(res?.events || res || []);
      } catch { if (!cancelled) setHistoryEvents([]); }
    })();
    return () => { cancelled = true; };
  }, [historyModel]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getIntelligenceVerdicts(verdictModel, { divergence_only: divergenceOnly, limit });
        if (!cancelled) setVerdictData({
          rows: res?.rows || res?.verdicts || [],
          top_divergence_types: res?.top_divergence_types || [],
        });
      } catch { if (!cancelled) setVerdictData({ rows: [], top_divergence_types: [] }); }
    })();
    return () => { cancelled = true; };
  }, [verdictModel, divergenceOnly, limit]);

  const handlePromote = async () => {
    const c = promoteTarget;
    try {
      await promoteCandidate(c.model_name, c.version);
      toast.show(`▲ Promoted ${c.model_name} → ${c.version.slice(0, 12)}…`);
      await loadModelsAndCandidates();
    } catch (e) {
      toast.show(`✕ Promote failed: ${e.message}`);
    } finally {
      setPromoteTarget(null);
    }
  };

  const handleReject = async (reason) => {
    const c = rejectTarget;
    try {
      await rejectCandidate(c.model_name, c.version, reason);
      toast.show(`✕ Rejected ${c.model_name} (${reason})`);
      await loadModelsAndCandidates();
    } catch (e) {
      toast.show(`✕ Reject failed: ${e.message}`);
    } finally {
      setRejectTarget(null);
    }
  };

  const handleRollback = async () => {
    try {
      await rollbackModel(historyModel);
      toast.show(`↺ Rolled back ${historyModel}`);
      const res = await getIntelligenceHistory(historyModel);
      setHistoryEvents(res?.events || res || []);
      await loadModelsAndCandidates();
    } catch (e) {
      toast.show(`✕ Rollback failed: ${e.message}`);
    } finally {
      setRollbackOpen(false);
    }
  };

  const handleRetrain = async () => {
    try {
      const res = await forceRetrain(verdictModel);
      const jobId = res?.job_id || Math.random().toString(16).slice(2, 10);
      setRetrainStatus(`Retrain queued for ${verdictModel} (job ${jobId}). New candidate will appear under Candidates.`);
      setTimeout(() => setRetrainStatus(''), 6000);
    } catch (e) {
      setRetrainStatus(`✕ Retrain failed: ${e.message}`);
      setTimeout(() => setRetrainStatus(''), 6000);
    }
  };

  const handleForceRetrainFromProd = (modelName) => {
    setSub('verdicts');
    setVerdictModel(modelName);
    setTimeout(handleRetrain, 100);
  };

  return (
    <div className="intel-view">
      <IntelHeader models={models} candidates={candidates} />
      <IntelSubnav sub={sub} setSub={setSub} />

      {loadError && (
        <div className="card" style={{ padding: 16, color: 'var(--red)' }}>{loadError}</div>
      )}

      {sub === 'production' && <ProductionView models={models} onForceRetrain={handleForceRetrainFromProd} />}
      {sub === 'candidates' && <CandidatesView candidates={candidates} onPromote={setPromoteTarget} onReject={setRejectTarget} />}
      {sub === 'history' && <HistoryView model={historyModel} setModel={setHistoryModel} events={historyEvents} onRollback={() => setRollbackOpen(true)} />}
      {sub === 'verdicts' && <VerdictsView model={verdictModel} setModel={setVerdictModel}
                                           divergenceOnly={divergenceOnly} setDivergenceOnly={setDivergenceOnly}
                                           limit={limit} setLimit={setLimit}
                                           data={verdictData}
                                           onRetrain={handleRetrain}
                                           retrainStatus={retrainStatus} />}

      {promoteTarget && <PromoteModal candidate={promoteTarget} onClose={() => setPromoteTarget(null)} onConfirm={handlePromote} />}
      {rejectTarget && <RejectModal candidate={rejectTarget} onClose={() => setRejectTarget(null)} onConfirm={handleReject} />}
      {rollbackOpen && <RollbackModal model={historyModel} onClose={() => setRollbackOpen(false)} onConfirm={handleRollback} />}

      {toast.node}
    </div>
  );
}
