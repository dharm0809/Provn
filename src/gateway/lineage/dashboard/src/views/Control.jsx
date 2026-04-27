/* Walacor Gateway — Control Plane view
   Manages attestations, policies, budgets, providers.
   Writes are gated behind an X-API-Key held in sessionStorage.

   Design notes:
   - Tabs: status · attestations · policies · budgets · providers
   - Read-only by default; unlock modal seeds sessionStorage.cp_api_key
   - All write helpers already exist in api.js — do not invent new ones.
*/

import React, { Fragment, useState, useEffect, useCallback } from 'react';
import {
  getControlStatus,
  getAttestations,
  revokeAttestation,
  removeAttestation,
  createAttestation,
  getPolicies,
  createPolicy,
  updatePolicy,
  deletePolicy,
  getBudgets,
  createBudget,
  deleteBudget,
  discoverModels,
  setControlKey,
  clearControlKey,
  hasControlKey,
  getContentPolicies,
  upsertContentPolicy,
  deleteContentPolicy,
  getPricing,
  upsertPricing,
  deletePricing,
  listTemplates,
  applyTemplate,
} from '../api';
import { timeAgo } from '../utils';
import '../styles/control.css';

// ─── format helpers ────────────────────────────────────────────

function fmtUsd(n) { return n == null ? '—' : '$' + n.toLocaleString(); }
function fmtTokens(n) {
  if (n == null) return '—';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(0) + 'k';
  return String(n);
}
function fmtUptime(sec) {
  if (sec == null) return '—';
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  return d + 'd ' + h + 'h';
}
function pct(a, b) {
  if (!b) return 0;
  return Math.min(100, Math.round((a / b) * 100));
}

/** Recent-activity feed for the status tab (last 72h) from store timestamps. */
function buildSyntheticControlEvents(attestationRows, policyRows, budgetRows, contentPolicyRows) {
  const cutoff = Date.now() - 72 * 3600 * 1000;
  const out = [];
  const rowMs = (row) => {
    const raw = row.updated_at || row.created_at;
    if (!raw) return null;
    const t = Date.parse(raw);
    return Number.isNaN(t) ? null : t;
  };
  const push = (t, kind, actor, label) => {
    if (t == null || t < cutoff) return;
    out.push({ kind, t, actor, label });
  };
  for (const a of attestationRows) {
    const t = rowMs(a);
    const st = (a.status || 'active').toLowerCase();
    push(t, `attestation.${st}`, 'embedded store', `${a.model_id || '—'} · ${a.provider || 'ollama'}`);
  }
  for (const p of policyRows) {
    const t = rowMs(p);
    push(t, 'policy.updated', 'embedded store', String(p.policy_name || p.policy_id || 'policy'));
  }
  for (const b of budgetRows) {
    const t = rowMs(b);
    const who = b.user ? `user ${b.user}` : 'tenant-wide';
    push(t, 'budget.updated', 'embedded store', `${b.period || 'monthly'} · ${who}`);
  }
  for (const c of contentPolicyRows) {
    const t = rowMs(c);
    push(t, 'policy.content', 'embedded store', `${c.analyzer_id || '—'} · ${c.category || '—'}`);
  }
  out.sort((a, b) => b.t - a.t);
  return out.slice(0, 50).map((e) => ({ ...e, t: new Date(e.t).toISOString() }));
}

// ─── small building blocks ─────────────────────────────────────

function Badge({ kind, children }) {
  return <span className={`cp-badge ${kind || ''}`}>{children}</span>;
}

function AuthStrip({ unlocked, onUnlock, onLock }) {
  return (
    <div className="cp-auth-strip">
      <span className={`cp-auth-chip ${unlocked ? 'is-unlocked' : 'is-locked'}`}>
        <span className="cp-dot" />
        {unlocked ? 'unlocked · writes allowed' : 'read-only · api key required'}
      </span>
      {unlocked
        ? <button className="cp-auth-btn" onClick={onLock}>lock</button>
        : <button className="cp-auth-btn" onClick={onUnlock}>unlock</button>}
    </div>
  );
}

function ReadonlyBanner({ onUnlock }) {
  return (
    <div className="cp-readonly-banner">
      <span className="cp-readonly-banner-icon">◇</span>
      <span className="cp-readonly-banner-text">
        <b>Read-only.</b> Enter the control-plane API key to create, edit, or revoke.
      </span>
      <button className="cp-readonly-banner-btn" onClick={onUnlock}>unlock</button>
    </div>
  );
}

function UnlockModal({ onClose, onSubmit }) {
  const [val, setVal] = useState('');
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);
  return (
    <div
      className="cp-modal-wrap"
      role="dialog"
      aria-modal="true"
      aria-labelledby="cp-unlock-title"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) e.preventDefault();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          e.preventDefault();
          e.stopPropagation();
        }
      }}
    >
      <div className="cp-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="cp-modal-eyebrow">◆ unlock writes</div>
        <h3 id="cp-unlock-title">Enter control-plane API key</h3>
        <p>
          Key is held in <code>sessionStorage</code> and cleared on tab close.
          Writes are gated server-side via <code>X-API-Key</code>.
        </p>
        <input
          className="cp-modal-input"
          type="password"
          autoFocus
          placeholder="cp_••••••••••••"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && val && onSubmit(val)}
        />
        <div className="cp-modal-row">
          <button className="cp-btn cp-btn-sm" onClick={onClose}>cancel</button>
          <button
            className="cp-btn cp-btn-primary cp-btn-sm"
            onClick={() => val && onSubmit(val)}
            disabled={!val}
          >unlock</button>
        </div>
      </div>
    </div>
  );
}

function Loading() {
  return (
    <div className="cp-loading">
      <div className="cp-loading-bar"><span /></div>
      <div className="cp-loading-label">loading…</div>
    </div>
  );
}

function Empty({ icon = '◇', title, body }) {
  return (
    <div className="cp-empty">
      <div className="cp-empty-icon">{icon}</div>
      <div className="cp-empty-title">{title}</div>
      {body && <div className="cp-empty-body">{body}</div>}
    </div>
  );
}

// ─── panels ────────────────────────────────────────────────────

function StatusPanel({ status, events, onOpenAuditLog }) {
  if (!status) return <Loading />;

  // Counters reflect what the control plane actually stores. Runtime
  // breach/health telemetry is not tracked backend-side here — the
  // Connections tab is the source of truth for provider liveness, and
  // budget breach detection lives in the request path, not the
  // control-plane store.
  const stats = [
    { k: 'Enforcement', v: status.enforcement_mode, foot: 'active mode', tone: 'ok' },
    { k: 'Attestations', v: status.attestations_active, foot: 'active in registry', tone: 'ok' },
    { k: 'Policies', v: status.policies_active, foot: 'configured', tone: 'ok' },
    { k: 'Budgets', v: status.budgets_active, foot: 'configured · runtime usage on Connections', tone: 'ok' },
    { k: 'Providers', v: status.provider_count, foot: 'configured · liveness on Connections', tone: 'ok' },
    { k: 'Analyzers', v: status.analyzer_count, foot: 'attached', tone: 'ok' },
    { k: 'Uptime', v: fmtUptime(status.uptime_seconds), foot: status.version, tone: 'ok' },
    { k: 'Last change', v: status.last_config_change ? timeAgo(status.last_config_change) : '—', foot: 'config ledger', tone: 'ok' },
  ];

  return (
    <>
      <div className="cp-stat-grid">
        {stats.map(s => (
          <div key={s.k} className="cp-stat">
            <div>
              <div className="cp-stat-label">{s.k}</div>
              <div className="cp-stat-value">{s.v}</div>
            </div>
            <div className={`cp-stat-foot ${s.tone}`}>
              <span className="cp-dot" /> {s.foot}
            </div>
          </div>
        ))}
      </div>

      <div className="cp-section">
        <div className="cp-section-head">
          <div className="cp-section-label"><span className="cp-dia">◆</span>recent control events</div>
          <span className="cp-section-meta">{(events || []).length} · last 72h</span>
        </div>
        {events && events.length ? (
          <ul className="cp-events">
            {events.map((ev, i) => {
              const tone = ev.kind.endsWith('.revoke') ? 'down' : ev.kind.endsWith('.breach') ? 'warn' : '';
              const icon =
                ev.kind.startsWith('attestation') ? '◆' :
                ev.kind.startsWith('policy') ? '◈' :
                ev.kind.startsWith('budget') ? '$' :
                ev.kind.startsWith('provider') ? '⟶' : '·';
              return (
                <li key={i} className="cp-event">
                  <span className="cp-event-time">{timeAgo(ev.t)}</span>
                  <span className={`cp-event-kind-icon ${tone}`}>{icon}</span>
                  <span className="cp-event-actor">{ev.actor}</span>
                  <span className="cp-event-label">
                    <span className="cp-event-kind-text">{ev.kind.replace('.', ' · ')}</span>
                    {ev.label}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : (
          <Empty title="No recent control events" body="Config changes over the last 72 hours appear here." />
        )}
        <div className="cp-section-foot">
          {onOpenAuditLog ? (
            <button type="button" className="cp-link-like" onClick={onOpenAuditLog}>view full audit log →</button>
          ) : (
            <span className="cp-muted">view full audit log →</span>
          )}
        </div>
      </div>
    </>
  );
}

function AttestationViewModal({ row, onClose }) {
  if (!row) return null;
  const r = row._raw || row;
  return (
    <div className="cp-modal-wrap" onClick={onClose}>
      <div className="cp-modal cp-modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="cp-modal-eyebrow">◆ attestation</div>
        <h3>{row.model}</h3>
        <p>Read-only snapshot from the control-plane store.</p>
        <dl className="cp-detail-dl">
          <dt>ID</dt><dd>{r.attestation_id || row.id}</dd>
          <dt>Model</dt><dd>{r.model_id || row.model}</dd>
          <dt>Provider</dt><dd>{r.provider || row.purpose}</dd>
          <dt>Status</dt><dd>{r.status || row.status}</dd>
          <dt>Verification</dt><dd>{r.verification_level || row.signer}</dd>
          <dt>Created</dt><dd>{r.created_at || '—'}</dd>
          <dt>Updated</dt><dd>{r.updated_at || '—'}</dd>
          <dt>Notes</dt><dd>{r.notes || row.notes || '—'}</dd>
        </dl>
        <div className="cp-modal-row">
          <button type="button" className="cp-btn cp-btn-sm" onClick={onClose}>close</button>
        </div>
      </div>
    </div>
  );
}

function AttestationCreateModal({ tenantId, onClose, onCreated }) {
  const [modelId, setModelId] = useState('');
  const [provider, setProvider] = useState('ollama');
  const [verificationLevel, setVerificationLevel] = useState('admin_attested');
  const [notes, setNotes] = useState('');

  const submit = async () => {
    if (!modelId.trim()) return;
    try {
      await createAttestation({
        model_id: modelId.trim(),
        provider: (provider || 'ollama').trim(),
        verification_level: verificationLevel,
        status: 'active',
        tenant_id: tenantId || '',
        notes: notes.trim(),
      });
      onCreated();
      onClose();
    } catch (e) {
      alert(e.message);
    }
  };

  return (
    <div className="cp-modal-wrap" onClick={onClose}>
      <div className="cp-modal cp-modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="cp-modal-eyebrow">◆ new attestation</div>
        <h3>Register a model attestation</h3>
        <p>Creates or updates the row for (tenant × provider × model). Requires a valid control-plane API key.</p>
        <input
          className="cp-modal-input"
          autoFocus
          placeholder="model_id (e.g. llama3.1:8b)"
          value={modelId}
          onChange={(e) => setModelId(e.target.value)}
        />
        <input
          className="cp-modal-input"
          placeholder="provider (default ollama)"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        />
        <select
          className="cp-modal-input"
          value={verificationLevel}
          onChange={(e) => setVerificationLevel(e.target.value)}
        >
          <option value="admin_attested">admin_attested</option>
          <option value="self_attested">self_attested</option>
        </select>
        <input
          className="cp-modal-input"
          placeholder="notes (optional)"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
        <div className="cp-modal-row">
          <button type="button" className="cp-btn cp-btn-sm" onClick={onClose}>cancel</button>
          <button type="button" className="cp-btn cp-btn-primary cp-btn-sm" onClick={submit} disabled={!modelId.trim()}>create</button>
        </div>
      </div>
    </div>
  );
}

function AttestationsPanel({ rows, canWrite, onUnlock, onRefresh, onMutate, tenantId }) {
  const [viewRow, setViewRow] = useState(null);
  const [createOpen, setCreateOpen] = useState(false);

  if (!rows) return <Loading />;

  const onRevoke = async (id) => {
    if (!window.confirm(`Revoke attestation ${id}?`)) return;
    try { await revokeAttestation(id); await onRefresh(); onMutate?.(); } catch (e) { alert(e.message); }
  };
  const onRemove = async (id) => {
    if (!window.confirm(`Permanently delete attestation ${id}? This cannot be undone.`)) return;
    if (!window.confirm('Confirm permanent delete (second step).')) return;
    try { await removeAttestation(id); await onRefresh(); onMutate?.(); } catch (e) { alert(e.message); }
  };

  return (
    <>
      <div className="cp-panel-head">
        <div className="cp-panel-head-left">
          <h2>Attestations <span className="cp-panel-head-count">{rows.length}</span></h2>
          <p>Signed bindings of (model × purpose). Requests without a matching active attestation are denied.</p>
        </div>
        <button
          type="button"
          className="cp-btn cp-btn-primary"
          disabled={!canWrite}
          onClick={!canWrite ? onUnlock : () => setCreateOpen(true)}
        >◆ new attestation</button>
      </div>

      <div className="cp-section">
        {rows.length === 0 ? (
          <Empty title="No attestations yet" body="Approve a discovered model to create your first attestation." />
        ) : (
          <table className="cp-table">
            <thead>
              <tr>
                <th style={{ width: 100 }}>ID</th>
                <th>Model</th>
                <th>Purpose</th>
                <th>Signer</th>
                <th>Created</th>
                <th>Updated</th>
                <th style={{ width: 100 }}>Status</th>
                <th style={{ width: 220 }}></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(a => (
                <tr key={a.id}>
                  <td><span className="cp-id">{a.id}</span></td>
                  <td>
                    <div className="cp-cell-stack">
                      <div className="cp-row-primary">{a.model}</div>
                      <div className="cp-fingerprint">{(a.fingerprint || '').slice(0, 20)}…</div>
                    </div>
                  </td>
                  <td className="cp-mono">{a.purpose}</td>
                  <td className="cp-mono cp-muted">{a.signer}</td>
                  <td className="cp-mono cp-dim">{a.created_at ? timeAgo(a.created_at) : '—'}</td>
                  <td className="cp-mono cp-dim">{a.updated_at ? timeAgo(a.updated_at) : '—'}</td>
                  <td><Badge kind={a.status}>{a.status}</Badge></td>
                  <td>
                    <div className="cp-row-actions">
                      <button type="button" className="cp-btn cp-btn-sm" onClick={() => setViewRow(a)}>view</button>
                      <button
                        type="button"
                        className="cp-btn cp-btn-sm"
                        disabled={!canWrite || a.status === 'revoked'}
                        onClick={() => onRevoke(a.id)}
                      >revoke</button>
                      <button
                        type="button"
                        className="cp-btn cp-btn-sm cp-btn-danger"
                        disabled={!canWrite}
                        onClick={() => onRemove(a.id)}
                      >delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {viewRow && <AttestationViewModal row={viewRow} onClose={() => setViewRow(null)} />}
      {createOpen && (
        <AttestationCreateModal
          tenantId={tenantId}
          onClose={() => setCreateOpen(false)}
          onCreated={() => { onRefresh(); onMutate?.(); }}
        />
      )}
    </>
  );
}

function PolicyEditorModal({ tenantId, mode, row, onClose, onSaved }) {
  const isCreate = mode === 'create';
  const [name, setName] = useState(row?.name || '');
  const [enforcement, setEnforcement] = useState(
    row ? (row.mode === 'enforce' ? 'blocking' : 'audit_only') : 'blocking',
  );
  const [description, setDescription] = useState(
    row?.applies_to && row.applies_to !== '—' ? row.applies_to : '',
  );

  const save = async () => {
    if (!name.trim()) return;
    try {
      if (isCreate) {
        await createPolicy({
          policy_name: name.trim(),
          enforcement_level: enforcement,
          rules: [],
          tenant_id: tenantId || '',
          description: description.trim(),
        });
      } else {
        await updatePolicy(row.id, {
          policy_name: name.trim(),
          enforcement_level: enforcement,
          description: description.trim(),
        });
      }
      onSaved();
      onClose();
    } catch (e) {
      alert(e.message);
    }
  };

  return (
    <div className="cp-modal-wrap" onClick={onClose}>
      <div className="cp-modal cp-modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="cp-modal-eyebrow">◆ {isCreate ? 'new policy' : 'edit policy'}</div>
        <h3>{isCreate ? 'Create governance policy' : 'Update policy'}</h3>
        <p>
          {isCreate
            ? 'Creates an empty rule container. Add rules via the API or future editor.'
            : 'Updates display metadata and enforcement mode. Rule JSON editing can follow via API.'}
        </p>
        <input
          className="cp-modal-input"
          autoFocus
          placeholder="policy name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <select
          className="cp-modal-input"
          value={enforcement}
          onChange={(e) => setEnforcement(e.target.value)}
        >
          <option value="blocking">blocking (enforce)</option>
          <option value="audit_only">audit_only (warn)</option>
        </select>
        <input
          className="cp-modal-input"
          placeholder="description (optional)"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <div className="cp-modal-row">
          <button type="button" className="cp-btn cp-btn-sm" onClick={onClose}>cancel</button>
          <button type="button" className="cp-btn cp-btn-primary cp-btn-sm" onClick={save} disabled={!name.trim()}>save</button>
        </div>
      </div>
    </div>
  );
}

function PoliciesPanel({
  rows, canWrite, onUnlock, onRefresh, cpRows, onRefreshCP, tplRows, onRefreshTpl, tenantId, onMutate,
}) {
  const [polModal, setPolModal] = useState(null);

  if (!rows) return <Loading />;

  const onDelete = async (id) => {
    if (!window.confirm(`Delete policy ${id}?`)) return;
    try {
      await deletePolicy(id);
      await onRefresh();
      onMutate?.();
    } catch (e) { alert(e.message); }
  };

  return (
    <>
      <div className="cp-panel-head">
        <div className="cp-panel-head-left">
          <h2>Policies <span className="cp-panel-head-count">{rows.length}</span></h2>
          <p>Rule sets evaluated on prompts, tool calls and responses. Enforce blocks; warn logs &amp; passes.</p>
        </div>
        <button
          type="button"
          className="cp-btn cp-btn-primary"
          disabled={!canWrite}
          onClick={!canWrite ? onUnlock : () => setPolModal({ mode: 'create' })}
        >◆ new policy</button>
      </div>

      <div className="cp-section">
        {rows.length === 0 ? (
          <Empty title="No policies configured" body="Policies evaluate prompts, tool calls, and responses." />
        ) : (
          <table className="cp-table">
            <thead>
              <tr>
                <th>Name</th>
                <th style={{ width: 60 }}>Ver</th>
                <th style={{ width: 100 }}>Mode</th>
                <th>Applies to</th>
                <th style={{ width: 70 }}>Rules</th>
                <th style={{ width: 140 }}>Last edit</th>
                <th style={{ width: 140 }}></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(p => (
                <tr key={p.id}>
                  <td>
                    <div className="cp-cell-stack">
                      <div className="cp-row-primary">{p.name}</div>
                      <div className="cp-fingerprint">{p.id}</div>
                    </div>
                  </td>
                  <td className="cp-mono">v{p.version}</td>
                  <td><Badge kind={p.mode === 'enforce' ? 'enforce' : 'warn'}>{p.mode}</Badge></td>
                  <td className="cp-mono cp-muted">{p.applies_to}</td>
                  <td className="cp-mono">{p.rules}</td>
                  <td className="cp-mono cp-dim">{p.last_edit ? timeAgo(p.last_edit) : '—'}</td>
                  <td>
                    <div className="cp-row-actions">
                      <button
                        type="button"
                        className="cp-btn cp-btn-sm"
                        disabled={!canWrite}
                        onClick={() => (canWrite ? setPolModal({ mode: 'edit', row: p }) : onUnlock())}
                      >edit</button>
                      <button
                        type="button"
                        className="cp-btn cp-btn-sm cp-btn-danger"
                        disabled={!canWrite}
                        onClick={() => onDelete(p.id)}
                      >delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {polModal && (
        <PolicyEditorModal
          key={polModal.mode + (polModal.row?.id || 'new')}
          tenantId={tenantId}
          mode={polModal.mode}
          row={polModal.row}
          onClose={() => setPolModal(null)}
          onSaved={() => { onRefresh(); onMutate?.(); }}
        />
      )}

      <ContentPoliciesSection rows={cpRows} canWrite={canWrite} onUnlock={onUnlock} onRefresh={onRefreshCP} onMutate={onMutate} />
      <TemplatesSection rows={tplRows} canWrite={canWrite} onUnlock={onUnlock} onRefresh={onRefreshTpl} onMutate={onMutate} />
    </>
  );
}

function ContentPoliciesSection({ rows, canWrite, onUnlock, onRefresh, onMutate }) {
  const [form, setForm] = useState(null);

  const startAdd = () => setForm({ analyzer_id: '', category: '', action: 'warn', threshold: '0.5' });
  const startEdit = (r) => setForm({
    id: r.id,
    analyzer_id: r.analyzer_id,
    category: r.category,
    action: r.action,
    threshold: String(r.threshold ?? 0.5),
  });
  const onSave = async () => {
    if (!form?.analyzer_id || !form?.category) return;
    try {
      await upsertContentPolicy({
        id: form.id,
        analyzer_id: form.analyzer_id.trim(),
        category: form.category.trim(),
        action: form.action,
        threshold: parseFloat(form.threshold) || 0,
      });
      setForm(null);
      onRefresh();
      onMutate?.();
    } catch (e) { alert(e.message); }
  };
  const onDelete = async (id) => {
    if (!window.confirm(`Delete content policy ${id}?`)) return;
    try { await deleteContentPolicy(id); onRefresh(); onMutate?.(); } catch (e) { alert(e.message); }
  };

  return (
    <div className="cp-section">
      <div className="cp-section-head">
        <div className="cp-section-label"><span className="cp-dia">◆</span>content-analyzer thresholds</div>
        <div className="cp-section-head-actions">
          <span className="cp-section-meta">{(rows || []).length} rules</span>
          <button
            className="cp-btn cp-btn-sm cp-btn-primary"
            disabled={!canWrite}
            onClick={!canWrite ? onUnlock : startAdd}
          >◆ add rule</button>
        </div>
      </div>

      {form && (
        <div className="cp-section cp-inline-form">
          <div className="cp-form-grid-5">
            <input className="cp-modal-input" placeholder="analyzer_id (e.g. walacor.pii.v1)" value={form.analyzer_id}
              onChange={e => setForm({ ...form, analyzer_id: e.target.value })} />
            <input className="cp-modal-input" placeholder="category (e.g. ssn, toxicity)" value={form.category}
              onChange={e => setForm({ ...form, category: e.target.value })} />
            <select className="cp-modal-input" value={form.action}
              onChange={e => setForm({ ...form, action: e.target.value })}>
              <option value="block">block</option>
              <option value="warn">warn</option>
              <option value="pass">pass</option>
            </select>
            <input className="cp-modal-input" placeholder="threshold" type="number" step="0.01" min="0" max="1" value={form.threshold}
              onChange={e => setForm({ ...form, threshold: e.target.value })} />
            <div className="cp-form-actions">
              <button type="button" className="cp-btn cp-btn-sm cp-btn-primary" onClick={onSave}>save</button>
              <button type="button" className="cp-btn cp-btn-sm" onClick={() => setForm(null)}>cancel</button>
            </div>
          </div>
        </div>
      )}

      {(!rows || rows.length === 0) ? (
        <Empty title="No analyzer thresholds configured" body="Configure BLOCK/WARN/PASS per analyzer and category." />
      ) : (
        <table className="cp-table">
          <thead>
            <tr>
              <th>Analyzer</th>
              <th>Category</th>
              <th style={{ width: 100 }}>Action</th>
              <th style={{ width: 110 }}>Threshold</th>
              <th style={{ width: 140 }}>Updated</th>
              <th style={{ width: 140 }}></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id}>
                <td><span className="cp-mono">{r.analyzer_id}</span></td>
                <td><span className="cp-mono cp-muted">{r.category}</span></td>
                <td><Badge kind={r.action === 'block' ? 'fail' : r.action === 'warn' ? 'warn' : 'ok'}>{r.action}</Badge></td>
                <td className="cp-mono">{Number(r.threshold ?? 0).toFixed(2)}</td>
                <td className="cp-mono cp-dim">{r.updated_at ? timeAgo(r.updated_at) : '—'}</td>
                <td>
                  <div className="cp-row-actions">
                    <button className="cp-btn cp-btn-sm" disabled={!canWrite} onClick={() => startEdit(r)}>edit</button>
                    <button className="cp-btn cp-btn-sm cp-btn-danger" disabled={!canWrite} onClick={() => onDelete(r.id)}>delete</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function TemplatesSection({ rows, canWrite, onUnlock, onRefresh, onMutate }) {
  const onApply = async (name) => {
    if (!window.confirm(`Apply template "${name}"? This will create the policies it contains.`)) return;
    try {
      const res = await applyTemplate(name);
      alert(`Applied. Created ${res.created_count ?? res.count ?? '?'} policies.`);
      onRefresh();
      onMutate?.();
    }
    catch (e) { alert(e.message); }
  };

  return (
    <div className="cp-section">
      <div className="cp-section-head">
        <div className="cp-section-label"><span className="cp-dia">◆</span>available templates</div>
        <span className="cp-section-meta">{(rows || []).length}</span>
      </div>
      {(!rows || rows.length === 0) ? (
        <Empty title="No templates available" body="Templates ship as JSON files under the gateway's templates directory." />
      ) : (
        <table className="cp-table">
          <thead>
            <tr>
              <th>Template</th>
              <th>Description</th>
              <th style={{ width: 80 }}>Version</th>
              <th style={{ width: 90 }}>Policies</th>
              <th style={{ width: 120 }}></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(t => (
              <tr key={t.name}>
                <td>
                  <div className="cp-cell-stack">
                    <div className="cp-row-primary">{t.display_name || t.name}</div>
                    <div className="cp-fingerprint">{t.name}</div>
                  </div>
                </td>
                <td className="cp-muted">{t.description || '—'}</td>
                <td className="cp-mono">v{t.version || '1.0'}</td>
                <td className="cp-mono">{t.policy_count ?? '—'}</td>
                <td>
                  <div className="cp-row-actions">
                    <button
                      className="cp-btn cp-btn-sm cp-btn-primary"
                      disabled={!canWrite}
                      onClick={() => canWrite ? onApply(t.name) : onUnlock()}
                    >apply →</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function PricingSection({ rows, canWrite, onUnlock, onRefresh, onMutate }) {
  const [form, setForm] = useState(null); // null | {model_pattern, input_cost_per_1k, output_cost_per_1k}

  const startAdd = () => setForm({ model_pattern: '', input_cost_per_1k: '', output_cost_per_1k: '' });
  const startEdit = (r) => setForm({
    pricing_id: r.pricing_id,
    model_pattern: r.model_pattern,
    input_cost_per_1k: String(r.input_cost_per_1k ?? ''),
    output_cost_per_1k: String(r.output_cost_per_1k ?? ''),
  });
  const onSave = async () => {
    if (!form?.model_pattern) return;
    try {
      await upsertPricing({
        pricing_id: form.pricing_id,
        model_pattern: form.model_pattern.trim(),
        input_cost_per_1k: parseFloat(form.input_cost_per_1k) || 0,
        output_cost_per_1k: parseFloat(form.output_cost_per_1k) || 0,
      });
      setForm(null);
      onRefresh();
      onMutate?.();
    } catch (e) { alert(e.message); }
  };
  const onDelete = async (id) => {
    if (!window.confirm(`Delete pricing for ${id}?`)) return;
    try { await deletePricing(id); onRefresh(); onMutate?.(); } catch (e) { alert(e.message); }
  };

  return (
    <div className="cp-section">
      <div className="cp-section-head">
        <div className="cp-section-label"><span className="cp-dia">◆</span>model pricing</div>
        <div className="cp-section-head-actions">
          <span className="cp-section-meta">{(rows || []).length} rules</span>
          <button
            type="button"
            className="cp-btn cp-btn-sm cp-btn-primary"
            disabled={!canWrite}
            onClick={!canWrite ? onUnlock : startAdd}
          >◆ add pricing</button>
        </div>
      </div>

      {form && (
        <div className="cp-section cp-inline-form">
          <div className="cp-form-grid-4">
            <input className="cp-modal-input" placeholder="model pattern (e.g. gpt-4o-*)" value={form.model_pattern}
              onChange={e => setForm({ ...form, model_pattern: e.target.value })} />
            <input className="cp-modal-input" placeholder="in $/1k" type="number" step="0.001" value={form.input_cost_per_1k}
              onChange={e => setForm({ ...form, input_cost_per_1k: e.target.value })} />
            <input className="cp-modal-input" placeholder="out $/1k" type="number" step="0.001" value={form.output_cost_per_1k}
              onChange={e => setForm({ ...form, output_cost_per_1k: e.target.value })} />
            <div className="cp-form-actions">
              <button type="button" className="cp-btn cp-btn-sm cp-btn-primary" onClick={onSave}>save</button>
              <button type="button" className="cp-btn cp-btn-sm" onClick={() => setForm(null)}>cancel</button>
            </div>
          </div>
        </div>
      )}

      {(!rows || rows.length === 0) ? (
        <Empty title="No pricing configured" body="Add a rule to convert token usage into USD spend on the budgets table above." />
      ) : (
        <table className="cp-table">
          <thead>
            <tr>
              <th>Model pattern</th>
              <th style={{ width: 130, textAlign: 'right' }}>Input $/1k</th>
              <th style={{ width: 130, textAlign: 'right' }}>Output $/1k</th>
              <th style={{ width: 80 }}>Currency</th>
              <th style={{ width: 140 }}>Updated</th>
              <th style={{ width: 140 }}></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.pricing_id}>
                <td><span className="cp-row-primary cp-mono">{r.model_pattern}</span></td>
                <td className="cp-mono" style={{ textAlign: 'right' }}>${Number(r.input_cost_per_1k).toFixed(3)}</td>
                <td className="cp-mono" style={{ textAlign: 'right' }}>${Number(r.output_cost_per_1k).toFixed(3)}</td>
                <td className="cp-mono cp-muted">{r.currency || 'USD'}</td>
                <td className="cp-mono cp-dim">{r.updated_at ? timeAgo(r.updated_at) : '—'}</td>
                <td>
                  <div className="cp-row-actions">
                    <button className="cp-btn cp-btn-sm" disabled={!canWrite} onClick={() => startEdit(r)}>edit</button>
                    <button className="cp-btn cp-btn-sm cp-btn-danger" disabled={!canWrite} onClick={() => onDelete(r.pricing_id)}>delete</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function BudgetEditorModal({ tenantId, initial, onClose, onSaved }) {
  const isEdit = Boolean(initial?.budget_id);
  const [userId, setUserId] = useState(initial?.user_id ?? '');
  const [period, setPeriod] = useState(initial?.period || 'monthly');
  const [maxTok, setMaxTok] = useState(String(initial?.max_tokens ?? ''));

  const save = async () => {
    try {
      await createBudget({
        budget_id: initial?.budget_id,
        tenant_id: tenantId || initial?.tenant_id || '',
        user_id: userId.trim(),
        period,
        max_tokens: parseInt(maxTok, 10) || 0,
      });
      onSaved();
      onClose();
    } catch (e) {
      alert(e.message);
    }
  };

  return (
    <div className="cp-modal-wrap" onClick={onClose}>
      <div className="cp-modal cp-modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="cp-modal-eyebrow">◆ {isEdit ? 'edit budget' : 'new budget'}</div>
        <h3>{isEdit ? 'Update token budget' : 'Create token budget'}</h3>
        <p>
          Budgets are keyed by (tenant, user scope, period). Leave user blank for a tenant-wide cap.
        </p>
        <input
          className="cp-modal-input"
          placeholder="user id (optional — blank = tenant-wide)"
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
        />
        <select className="cp-modal-input" value={period} onChange={(e) => setPeriod(e.target.value)}>
          <option value="daily">daily</option>
          <option value="weekly">weekly</option>
          <option value="monthly">monthly</option>
          <option value="total">total</option>
        </select>
        <input
          className="cp-modal-input"
          type="number"
          min="0"
          placeholder="max_tokens"
          value={maxTok}
          onChange={(e) => setMaxTok(e.target.value)}
        />
        <div className="cp-modal-row">
          <button type="button" className="cp-btn cp-btn-sm" onClick={onClose}>cancel</button>
          <button type="button" className="cp-btn cp-btn-primary cp-btn-sm" onClick={save}>save</button>
        </div>
      </div>
    </div>
  );
}

function BudgetsPanel({ rows, canWrite, onUnlock, onRefresh, pricing, onRefreshPricing, tenantId, onMutate }) {
  const [budgetModal, setBudgetModal] = useState(null);

  if (!rows) return <Loading />;

  const onDelete = async (id) => {
    if (!window.confirm(`Delete budget ${id}?`)) return;
    try {
      await deleteBudget(id);
      await onRefresh();
      onMutate?.();
    } catch (e) { alert(e.message); }
  };

  return (
    <>
      <div className="cp-panel-head">
        <div className="cp-panel-head-left">
          <h2>Budgets <span className="cp-panel-head-count">{rows.length}</span></h2>
          <p>USD + token caps. When a budget breaches, the gateway fails-closed for that scope until lifted.</p>
        </div>
        <button
          type="button"
          className="cp-btn cp-btn-primary"
          disabled={!canWrite}
          onClick={!canWrite ? onUnlock : () => setBudgetModal({ mode: 'create' })}
        >◆ new budget</button>
      </div>

      <div className="cp-section">
        {rows.length === 0 ? (
          <Empty title="No budgets configured" body="Cap spend per user, team, or model." />
        ) : (
          <table className="cp-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Scope</th>
                <th style={{ width: 80 }}>Window</th>
                <th style={{ width: 200 }}>Token cap</th>
                <th style={{ width: 160 }}></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(b => {
                return (
                  <tr key={b.id}>
                    <td>
                      <div className="cp-cell-stack">
                        <div className="cp-row-primary">{b.name}</div>
                        <div className="cp-fingerprint">updated {b.expires ? timeAgo(b.expires) : '—'}</div>
                      </div>
                    </td>
                    <td className="cp-mono cp-muted">{b.scope}</td>
                    <td className="cp-mono">{b.window}</td>
                    <td className="cp-mono">{b.tokens_cap != null ? fmtTokens(b.tokens_cap) : '—'}</td>
                    <td>
                      <div className="cp-row-actions">
                        <button
                          type="button"
                          className="cp-btn cp-btn-sm"
                          disabled={!canWrite}
                          onClick={() => (canWrite ? setBudgetModal({ mode: 'edit', row: b }) : onUnlock())}
                        >edit</button>
                        <button
                          type="button"
                          className="cp-btn cp-btn-sm cp-btn-danger"
                          disabled={!canWrite}
                          onClick={() => onDelete(b.id)}
                        >delete</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {budgetModal && (
        <BudgetEditorModal
          key={(budgetModal.row?.id || 'new') + budgetModal.mode}
          tenantId={tenantId}
          initial={budgetModal.mode === 'edit' && budgetModal.row?._raw ? {
            budget_id: budgetModal.row._raw.budget_id,
            tenant_id: budgetModal.row._raw.tenant_id,
            user_id: budgetModal.row._raw.user || '',
            period: budgetModal.row._raw.period,
            max_tokens: budgetModal.row._raw.max_tokens,
          } : null}
          onClose={() => setBudgetModal(null)}
          onSaved={() => { onRefresh(); onMutate?.(); }}
        />
      )}

      <PricingSection rows={pricing} canWrite={canWrite} onUnlock={onUnlock} onRefresh={onRefreshPricing} onMutate={onMutate} />
    </>
  );
}

function ProvidersPanel({ data, canWrite, onUnlock, onRefresh }) {
  const [expandedProvider, setExpandedProvider] = useState(null);
  if (!data) return <Loading />;

  const providers = data.providers || [];
  const models = data.discovered_models || [];
  const pendingModels = models.filter(m => !m.attested);
  const attestedByProvider = models.reduce((acc, m) => {
    if (!m.attested) return acc;
    (acc[m.provider] = acc[m.provider] || []).push(m);
    return acc;
  }, {});

  const onDiscover = async () => {
    try { await discoverModels(); onRefresh(); } catch (e) { alert(e.message); }
  };

  const onAttestOne = async (m) => {
    try {
      await createAttestation({ model_id: m.id, provider: m.provider, status: 'active' });
      onRefresh();
    } catch (e) { alert(e.message); }
  };

  const onAttestAll = async () => {
    if (!pendingModels.length) return;
    if (!window.confirm(`Register all ${pendingModels.length} discovered models?`)) return;
    try {
      await Promise.all(pendingModels.map(m =>
        createAttestation({ model_id: m.id, provider: m.provider, status: 'active' })
          .catch(e => ({ error: String(e?.message || e), id: m.id }))
      ));
      onRefresh();
    } catch (e) { alert(e.message); }
  };

  return (
    <>
      <div className="cp-panel-head">
        <div className="cp-panel-head-left">
          <h2>Providers &amp; discovery <span className="cp-panel-head-count">{providers.length}</span></h2>
          <p>Upstream LLM endpoints and the models discovered on each. New models appear here before they can be attested.</p>
        </div>
        <button
          className="cp-btn cp-btn-primary"
          disabled={!canWrite}
          onClick={!canWrite ? onUnlock : onDiscover}
        >◆ discover now</button>
      </div>

      <div className="cp-section">
        <div className="cp-section-head">
          <div className="cp-section-label"><span className="cp-dia">◆</span>endpoints</div>
        </div>
        {providers.length === 0 ? (
          <Empty title="No providers configured" body="Add an upstream LLM endpoint to start discovering models." />
        ) : (
          <table className="cp-table">
            <thead>
              <tr>
                <th>Provider</th>
                <th>Endpoint</th>
                <th style={{ width: 90 }}>Latency</th>
                <th style={{ width: 100 }}>Discovered</th>
                <th style={{ width: 100 }}>Attested</th>
                <th style={{ width: 100 }}>Status</th>
                <th style={{ width: 140 }}></th>
              </tr>
            </thead>
            <tbody>
              {providers.map(p => {
                const isOpen = expandedProvider === p.name;
                const attested = attestedByProvider[p.name] || [];
                return (
                  <Fragment key={p.id}>
                    <tr>
                      <td><span className="cp-row-primary">{p.name}</span></td>
                      <td className="cp-mono cp-muted">{p.endpoint}</td>
                      <td className="cp-mono">{p.latency_ms != null ? `${p.latency_ms}ms` : '—'}</td>
                      <td className="cp-mono">{p.discovered}</td>
                      <td className="cp-mono">{p.attested}</td>
                      <td><Badge kind={p.status}>{p.status}</Badge></td>
                      <td>
                        <div className="cp-row-actions">
                          <button
                            className="cp-btn cp-btn-sm"
                            onClick={() => setExpandedProvider(isOpen ? null : p.name)}
                            aria-expanded={isOpen}
                          >{isOpen ? 'hide' : 'view'}</button>
                          <button className="cp-btn cp-btn-sm" disabled={!canWrite}>re-sync</button>
                        </div>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="cp-row-expansion">
                        <td colSpan={7}>
                          <div className="cp-expansion-inner">
                            <div className="cp-expansion-head">
                              <span className="cp-section-label">
                                <span className="cp-dia">◆</span>attested models on {p.name}
                              </span>
                              <span className="cp-section-meta">{attested.length} active</span>
                            </div>
                            {attested.length === 0 ? (
                              <Empty
                                title="No attested models yet"
                                body={`Attest a ${p.name} model below to make it available through the gateway.`}
                              />
                            ) : (
                              <table className="cp-table cp-table-inner">
                                <thead>
                                  <tr>
                                    <th>Model</th>
                                    <th style={{ width: 160 }}>Context</th>
                                    <th style={{ width: 160 }}>First seen</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {attested.map((m, i) => (
                                    <tr key={i}>
                                      <td className="cp-mono">{m.id}</td>
                                      <td className="cp-mono">{m.context != null ? m.context.toLocaleString() + ' tok' : '—'}</td>
                                      <td className="cp-mono cp-dim">{m.seen_at ? timeAgo(m.seen_at) : '—'}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="cp-section">
        <div className="cp-section-head">
          <div className="cp-section-label"><span className="cp-dia">◆</span>newly discovered models · not yet attested</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span className="cp-section-meta">{pendingModels.length} pending</span>
            {pendingModels.length > 0 && (
              <button
                className="cp-btn cp-btn-sm cp-btn-primary"
                disabled={!canWrite}
                onClick={!canWrite ? onUnlock : onAttestAll}
              >register all →</button>
            )}
          </div>
        </div>
        {pendingModels.length === 0 ? (
          <Empty title="Nothing pending" body="All discovered models are attested." />
        ) : (
          <table className="cp-table">
            <thead>
              <tr>
                <th>Provider</th>
                <th>Model</th>
                <th style={{ width: 120 }}>Context</th>
                <th style={{ width: 140 }}>First seen</th>
                <th style={{ width: 160 }}></th>
              </tr>
            </thead>
            <tbody>
              {pendingModels.map((m, i) => (
                <tr key={i}>
                  <td><span className="cp-row-primary">{m.provider}</span></td>
                  <td className="cp-mono">{m.id}</td>
                  <td className="cp-mono">{m.context != null ? m.context.toLocaleString() + ' tok' : '—'}</td>
                  <td className="cp-mono cp-dim">{m.seen_at ? timeAgo(m.seen_at) : '—'}</td>
                  <td>
                    <div className="cp-row-actions">
                      <button
                        className="cp-btn cp-btn-sm cp-btn-primary"
                        disabled={!canWrite}
                        onClick={!canWrite ? onUnlock : () => onAttestOne(m)}
                      >attest →</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

// ─── main view ─────────────────────────────────────────────────

export default function Control({ navigate }) {
  const [tab, setTab] = useState('status');
  const [unlocked, setUnlocked] = useState(hasControlKey());
  const [modal, setModal] = useState(false);
  const [tenantId, setTenantId] = useState('');

  const [status, setStatus] = useState(null);
  const [events, setEvents] = useState(null);
  const [attestations, setAttestations] = useState(null);
  const [policies, setPolicies] = useState(null);
  const [budgets, setBudgets] = useState(null);
  const [providers, setProviders] = useState(null);
  const [contentPolicies, setContentPolicies] = useState(null);
  const [pricing, setPricing] = useState(null);
  const [templates, setTemplates] = useState(null);

  // ── Adapters: real backend shapes → panel-expected shapes ────────
  //
  // The designer drew the UI against a future data contract; today's
  // endpoints return different field names and no pre-aggregated counts.
  // These adapters keep the view code untouched while wiring up the
  // real /v1/control/* endpoints.

  const loadStatus = useCallback(async () => {
    try {
      const [s, atts, pols, buds, cpRes] = await Promise.all([
        getControlStatus(),
        getAttestations().catch(() => ({ attestations: [] })),
        getPolicies().catch(() => ({ policies: [] })),
        getBudgets().catch(() => ({ budgets: [] })),
        getContentPolicies().catch(() => ({ policies: [] })),
      ]);
      setTenantId(String(s.tenant_id || ''));
      const attRows = s.attestations || atts.attestations || [];
      const polRows = pols.policies || [];
      const budRows = buds.budgets || [];
      const cpRows = cpRes.policies || [];
      const providerList = Array.isArray(s.providers) ? s.providers : [];
      const analyzerCount = s.content_analyzers?.count
        ?? (Array.isArray(s.content_analyzers) ? s.content_analyzers.length : 0);
      setStatus({
        enforcement_mode: s.enforcement_mode ?? (s.skip_governance ? 'audit' : 'enforce'),
        attestations_active: attRows.filter(a => a.status === 'active').length,
        policies_active: polRows.filter(p => (p.status ?? 'active') === 'active').length,
        budgets_active: budRows.length,
        provider_count: providerList.length,
        analyzer_count: analyzerCount,
        uptime_seconds: s.uptime_seconds,
        version: s.gateway_id ? `gw · ${String(s.gateway_id).slice(0, 8)}` : 'gateway',
        last_config_change: s.policy_cache?.last_sync || null,
      });
      setEvents(buildSyntheticControlEvents(attRows, polRows, budRows, cpRows));
    } catch (e) {
      setStatus({});
      setEvents([]);
      setTenantId('');
    }
  }, []);

  const loadAttestations = useCallback(async () => {
    try {
      const d = await getAttestations();
      const rows = d.attestations || d.rows || d || [];
      // Map backend { attestation_id, model_id, provider, status,
      // verification_level, notes, created_at, updated_at }
      // onto what the panel reads.
      setAttestations(rows.map(a => ({
        id: a.attestation_id,
        model: a.model_id,
        purpose: a.provider,
        signer: a.verification_level,
        fingerprint: a.attestation_id,
        created_at: a.created_at,
        updated_at: a.updated_at,
        status: a.status,
        notes: a.notes || '',
        _raw: a,
      })));
    } catch { setAttestations([]); }
  }, []);

  const loadPolicies = useCallback(async () => {
    try {
      const d = await getPolicies();
      const rows = d.policies || d.rows || d || [];
      setPolicies(rows.map(p => {
        let ruleCount = 0;
        try {
          const parsed = typeof p.rules_json === 'string' ? JSON.parse(p.rules_json) : p.rules_json;
          if (Array.isArray(parsed)) ruleCount = parsed.length;
        } catch { /* ignore */ }
        return {
          id: p.policy_id,
          name: p.policy_name,
          version: 1,
          mode: p.enforcement_level === 'blocking' ? 'enforce' : 'warn',
          applies_to: p.description || '—',
          rules: ruleCount,
          last_edit: p.updated_at,
        };
      }));
    } catch { setPolicies([]); }
  }, []);

  const loadBudgets = useCallback(async () => {
    try {
      const d = await getBudgets();
      const rows = d.budgets || d.rows || d || [];
      setBudgets(rows.map(b => ({
        id: b.budget_id,
        name: b.user || b.tenant_id || b.budget_id,
        scope: `tenant=${b.tenant_id}${b.user ? ` · user=${b.user}` : ''}`,
        window: b.period,
        tokens_cap: b.max_tokens,
        expires: b.updated_at,
        _raw: b,
      })));
    } catch { setBudgets([]); }
  }, []);

  const loadProviders = useCallback(async () => {
    try {
      const [disc, statusResp, atts] = await Promise.all([
        discoverModels(),
        getControlStatus().catch(() => ({})),
        getAttestations().catch(() => ({ attestations: [] })),
      ]);
      const discModels = disc.models || [];
      const attRows = atts.attestations || [];
      const attestedIds = new Set(attRows.map(a => a.model_id));
      const cfgProviders = Array.isArray(statusResp.providers) ? statusResp.providers : [];

      // Group discovered models by provider for the per-provider table.
      const byProv = new Map();
      for (const m of discModels) {
        const list = byProv.get(m.provider) || [];
        list.push(m);
        byProv.set(m.provider, list);
      }

      // If the status endpoint didn't list a provider but we found models
      // from it, still surface it. (Belt-and-braces for misconfigured setups.)
      const allProvNames = new Set([
        ...cfgProviders.map(p => p.name),
        ...byProv.keys(),
      ]);

      const providers = Array.from(allProvNames).map((name, idx) => {
        const cfg = cfgProviders.find(p => p.name === name) || {};
        const models = byProv.get(name) || [];
        return {
          id: `${name}-${idx}`,
          name,
          endpoint: cfg.url || '—',
          latency_ms: null,
          discovered: models.length,
          attested: models.filter(m => attestedIds.has(m.model_id)).length,
          status: 'ok',
        };
      });

      const discovered_models = discModels.map(m => ({
        provider: m.provider,
        id: m.model_id,
        context: m.context_length || null,
        seen_at: m.seen_at || null,
        attested: attestedIds.has(m.model_id) || m.registered === true,
      }));

      setProviders({ providers, discovered_models });
    } catch { setProviders({ providers: [], discovered_models: [] }); }
  }, []);

  // New nested sections: content policies + pricing + templates.
  const loadContentPolicies = useCallback(async () => {
    try { const d = await getContentPolicies(); setContentPolicies(d.policies || d.rows || d || []); }
    catch { setContentPolicies([]); }
  }, []);
  const loadPricing = useCallback(async () => {
    try { const d = await getPricing(); setPricing(d.pricing || d.rows || d || []); }
    catch { setPricing([]); }
  }, []);
  const loadTemplates = useCallback(async () => {
    try { const d = await listTemplates(); setTemplates(d.templates || d.rows || d || []); }
    catch { setTemplates([]); }
  }, []);

  useEffect(() => { loadStatus(); }, [loadStatus]);
  useEffect(() => {
    if (tab === 'attestations' && attestations == null) loadAttestations();
    if (tab === 'policies') {
      if (policies == null) loadPolicies();
      if (contentPolicies == null) loadContentPolicies();
      if (templates == null) loadTemplates();
    }
    if (tab === 'budgets') {
      if (budgets == null) loadBudgets();
      if (pricing == null) loadPricing();
    }
    if (tab === 'providers' && providers == null) loadProviders();
  }, [tab, attestations, policies, budgets, providers, contentPolicies, pricing, templates,
      loadAttestations, loadPolicies, loadBudgets, loadProviders,
      loadContentPolicies, loadPricing, loadTemplates]);

  const onUnlock = () => setModal(true);
  const onSubmitKey = (key) => {
    setControlKey(key);
    setUnlocked(true);
    setModal(false);
  };
  const onLock = () => {
    clearControlKey();
    setUnlocked(false);
  };

  const TabBtn = ({ k, label, count }) => (
    <button className={`cp-tab ${tab === k ? 'is-active' : ''}`} onClick={() => setTab(k)}>
      <span className="cp-tab-label">{label}</span>
      {count != null && <span className="cp-tab-count">{count}</span>}
    </button>
  );

  return (
    <div className="cp-page" data-screen-label="Control">
      <div className="cp-intro card card-accent">
        <div className="cp-intro-body">
          <div className="cp-intro-eyebrow">
            <span className="cp-dia">◆</span>walacor gateway
            <span className="cp-eyebrow-sep">·</span>governance
          </div>
          <h1>Control plane</h1>
          <p className="cp-intro-sub">
            Manage the rules the gateway enforces — signed attestations, governance
            policies, spend budgets and the provider surface they apply to.
          </p>
        </div>
        <AuthStrip unlocked={unlocked} onUnlock={onUnlock} onLock={onLock} />
      </div>

      <div className="cp-tabs">
        <TabBtn k="status" label="status" />
        <TabBtn k="attestations" label="attestations" count={attestations?.length} />
        <TabBtn k="policies" label="policies" count={policies?.length} />
        <TabBtn k="budgets" label="budgets" count={budgets?.length} />
        <TabBtn k="providers" label="providers" count={providers?.providers?.length} />
      </div>

      {!unlocked && tab !== 'status' && <ReadonlyBanner onUnlock={onUnlock} />}

      <div className="cp-tab-panel">
        {tab === 'status' && (
          <StatusPanel
            status={status}
            events={events}
            onOpenAuditLog={typeof navigate === 'function' ? () => navigate('attempts') : undefined}
          />
        )}
        {tab === 'attestations' && (
          <AttestationsPanel
            rows={attestations}
            canWrite={unlocked}
            onUnlock={onUnlock}
            onRefresh={loadAttestations}
            onMutate={loadStatus}
            tenantId={tenantId}
          />
        )}
        {tab === 'policies' && (
          <PoliciesPanel
            rows={policies}
            canWrite={unlocked}
            onUnlock={onUnlock}
            onRefresh={loadPolicies}
            cpRows={contentPolicies}
            onRefreshCP={loadContentPolicies}
            tplRows={templates}
            onRefreshTpl={loadTemplates}
            tenantId={tenantId}
            onMutate={loadStatus}
          />
        )}
        {tab === 'budgets' && (
          <BudgetsPanel
            rows={budgets}
            canWrite={unlocked}
            onUnlock={onUnlock}
            onRefresh={loadBudgets}
            pricing={pricing}
            onRefreshPricing={loadPricing}
            tenantId={tenantId}
            onMutate={loadStatus}
          />
        )}
        {tab === 'providers' && <ProvidersPanel data={providers} canWrite={unlocked} onUnlock={onUnlock} onRefresh={loadProviders} />}
      </div>

      {modal && <UnlockModal onClose={() => setModal(false)} onSubmit={onSubmitKey} />}
    </div>
  );
}
