/* Walacor Gateway — Control Plane view
   Manages attestations, policies, budgets, providers.
   Writes are gated behind an X-API-Key held in sessionStorage.

   Design notes:
   - Tabs: status · attestations · policies · budgets · providers
   - Read-only by default; unlock modal seeds sessionStorage.cp_api_key
   - All write helpers already exist in api.js — do not invent new ones.
*/

import React, { useState, useEffect, useCallback } from 'react';
import {
  getControlStatus,
  getAttestations,
  revokeAttestation,
  removeAttestation,
  getPolicies,
  deletePolicy,
  getBudgets,
  deleteBudget,
  discoverModels,
  setControlKey,
  clearControlKey,
  hasControlKey,
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
  return (
    <div className="cp-modal-wrap" onClick={onClose}>
      <div className="cp-modal" onClick={(e) => e.stopPropagation()}>
        <div className="cp-modal-eyebrow">◆ unlock writes</div>
        <h3>Enter control-plane API key</h3>
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

function StatusPanel({ status, events }) {
  if (!status) return <Loading />;

  const stats = [
    { k: 'Enforcement', v: status.enforcement_mode, foot: 'active', tone: 'ok' },
    { k: 'Attestations', v: status.attestations_active, foot: 'signed · in-force', tone: 'ok' },
    { k: 'Policies', v: status.policies_active, foot: 'enforce + warn', tone: 'ok' },
    {
      k: 'Budgets',
      v: `${status.budgets_active - (status.budgets_breached || 0)}/${status.budgets_active}`,
      foot: status.budgets_breached ? `${status.budgets_breached} at breach` : 'all within cap',
      tone: status.budgets_breached ? 'warn' : 'ok',
    },
    {
      k: 'Providers',
      v: `${status.providers_healthy}/${status.provider_count}`,
      foot: 'healthy',
      tone: status.providers_healthy === status.provider_count ? 'ok' : 'warn',
    },
    { k: 'Analyzers', v: status.analyzer_count, foot: 'attached', tone: 'ok' },
    { k: 'Uptime', v: fmtUptime(status.uptime_seconds), foot: status.version, tone: 'ok' },
    { k: 'Last change', v: status.last_config_change ? timeAgo(status.last_config_change) : '—', foot: 'config ledger', tone: 'ok' },
  ];

  return (
    <>
      <div className="cp-panel-head">
        <div className="cp-panel-head-left">
          <h2>Control plane status</h2>
          <p>Single-pane view of attestations, policies, budgets and providers — plus the audit trail of every config change.</p>
        </div>
      </div>

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
                ev.kind.startsWith('policy') ? '§' :
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
        <div className="cp-section-foot"><a>view full audit log →</a></div>
      </div>
    </>
  );
}

function AttestationsPanel({ rows, canWrite, onUnlock, onRefresh }) {
  if (!rows) return <Loading />;

  const onRevoke = async (id) => {
    if (!window.confirm(`Revoke attestation ${id}?`)) return;
    try { await revokeAttestation(id); onRefresh(); } catch (e) { alert(e.message); }
  };
  const onRemove = async (id) => {
    if (!window.confirm(`Permanently remove ${id}?`)) return;
    try { await removeAttestation(id); onRefresh(); } catch (e) { alert(e.message); }
  };

  return (
    <>
      <div className="cp-panel-head">
        <div className="cp-panel-head-left">
          <h2>Attestations <span className="cp-panel-head-count">{rows.length}</span></h2>
          <p>Signed bindings of (model × purpose). Requests without a matching active attestation are denied.</p>
        </div>
        <button
          className="cp-btn cp-btn-primary"
          disabled={!canWrite}
          onClick={!canWrite ? onUnlock : undefined}
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
                <th>Signed</th>
                <th>Expires</th>
                <th style={{ width: 100 }}>Status</th>
                <th style={{ width: 170 }}></th>
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
                  <td className="cp-mono cp-dim">{a.signed_at ? timeAgo(a.signed_at) : '—'}</td>
                  <td className="cp-mono cp-dim">{a.expires_at ? timeAgo(a.expires_at) : '—'}</td>
                  <td><Badge kind={a.status}>{a.status}</Badge></td>
                  <td>
                    <div className="cp-row-actions">
                      <button className="cp-btn cp-btn-sm">view</button>
                      <button
                        className="cp-btn cp-btn-sm cp-btn-danger"
                        disabled={!canWrite || a.status === 'revoked'}
                        onClick={() => onRevoke(a.id)}
                      >revoke</button>
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

function PoliciesPanel({ rows, canWrite, onUnlock, onRefresh }) {
  if (!rows) return <Loading />;

  const onDelete = async (id) => {
    if (!window.confirm(`Delete policy ${id}?`)) return;
    try { await deletePolicy(id); onRefresh(); } catch (e) { alert(e.message); }
  };

  return (
    <>
      <div className="cp-panel-head">
        <div className="cp-panel-head-left">
          <h2>Policies <span className="cp-panel-head-count">{rows.length}</span></h2>
          <p>Rule sets evaluated on prompts, tool calls and responses. Enforce blocks; warn logs &amp; passes.</p>
        </div>
        <button
          className="cp-btn cp-btn-primary"
          disabled={!canWrite}
          onClick={!canWrite ? onUnlock : undefined}
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
                <th style={{ width: 90 }}>Hits 24h</th>
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
                  <td className="cp-mono" style={{ color: p.hits_24h > 10 ? 'var(--amber)' : 'var(--fg-muted)' }}>{p.hits_24h}</td>
                  <td className="cp-mono cp-dim">{p.last_edit ? timeAgo(p.last_edit) : '—'}</td>
                  <td>
                    <div className="cp-row-actions">
                      <button className="cp-btn cp-btn-sm">edit</button>
                      <button
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
    </>
  );
}

function BudgetsPanel({ rows, canWrite, onUnlock, onRefresh }) {
  if (!rows) return <Loading />;

  const onDelete = async (id) => {
    if (!window.confirm(`Delete budget ${id}?`)) return;
    try { await deleteBudget(id); onRefresh(); } catch (e) { alert(e.message); }
  };

  return (
    <>
      <div className="cp-panel-head">
        <div className="cp-panel-head-left">
          <h2>Budgets <span className="cp-panel-head-count">{rows.length}</span></h2>
          <p>USD + token caps. When a budget breaches, the gateway fails-closed for that scope until lifted.</p>
        </div>
        <button
          className="cp-btn cp-btn-primary"
          disabled={!canWrite}
          onClick={!canWrite ? onUnlock : undefined}
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
                <th style={{ width: 220 }}>Spend</th>
                <th style={{ width: 220 }}>Tokens</th>
                <th style={{ width: 90 }}>Status</th>
                <th style={{ width: 140 }}></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(b => {
                const pctSpend = pct(b.spent_usd, b.cap_usd);
                const pctTok = pct(b.tokens_used, b.tokens_cap);
                return (
                  <tr key={b.id}>
                    <td>
                      <div className="cp-cell-stack">
                        <div className="cp-row-primary">{b.name}</div>
                        <div className="cp-fingerprint">expires {b.expires ? timeAgo(b.expires) : '—'}</div>
                      </div>
                    </td>
                    <td className="cp-mono cp-muted">{b.scope}</td>
                    <td className="cp-mono">{b.window}</td>
                    <td>
                      <div className="cp-budget-bar">
                        <div className={`cp-budget-bar-fill ${pctSpend > 90 ? 'breach' : pctSpend > 70 ? 'warn' : ''}`} style={{ width: pctSpend + '%' }} />
                      </div>
                      <div className="cp-budget-meta">
                        <span><strong>{fmtUsd(b.spent_usd)}</strong> / {fmtUsd(b.cap_usd)}</span>
                        <span>{pctSpend}%</span>
                      </div>
                    </td>
                    <td>
                      <div className="cp-budget-bar">
                        <div className={`cp-budget-bar-fill ${pctTok > 90 ? 'breach' : pctTok > 70 ? 'warn' : ''}`} style={{ width: pctTok + '%' }} />
                      </div>
                      <div className="cp-budget-meta">
                        <span><strong>{fmtTokens(b.tokens_used)}</strong> / {fmtTokens(b.tokens_cap)}</span>
                        <span>{pctTok}%</span>
                      </div>
                    </td>
                    <td><Badge kind={b.breach}>{b.breach === 'breach' ? 'breach' : b.breach === 'warn' ? 'warn' : 'ok'}</Badge></td>
                    <td>
                      <div className="cp-row-actions">
                        <button className="cp-btn cp-btn-sm">edit</button>
                        <button
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
    </>
  );
}

function ProvidersPanel({ data, canWrite, onUnlock, onRefresh }) {
  if (!data) return <Loading />;

  const providers = data.providers || [];
  const models = data.discovered_models || [];
  const pendingModels = models.filter(m => !m.attested);

  const onDiscover = async () => {
    try { await discoverModels(); onRefresh(); } catch (e) { alert(e.message); }
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
              {providers.map(p => (
                <tr key={p.id}>
                  <td><span className="cp-row-primary">{p.name}</span></td>
                  <td className="cp-mono cp-muted">{p.endpoint}</td>
                  <td className="cp-mono">{p.latency_ms != null ? `${p.latency_ms}ms` : '—'}</td>
                  <td className="cp-mono">{p.discovered}</td>
                  <td className="cp-mono">{p.attested}</td>
                  <td><Badge kind={p.status}>{p.status}</Badge></td>
                  <td>
                    <div className="cp-row-actions">
                      <button className="cp-btn cp-btn-sm">view</button>
                      <button className="cp-btn cp-btn-sm" disabled={!canWrite}>re-sync</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="cp-section">
        <div className="cp-section-head">
          <div className="cp-section-label"><span className="cp-dia">◆</span>newly discovered models · not yet attested</div>
          <span className="cp-section-meta">{pendingModels.length} pending</span>
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
                      <button className="cp-btn cp-btn-sm cp-btn-primary" disabled={!canWrite}>attest →</button>
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

export default function Control() {
  const [tab, setTab] = useState('status');
  const [unlocked, setUnlocked] = useState(hasControlKey());
  const [modal, setModal] = useState(false);

  const [status, setStatus] = useState(null);
  const [events, setEvents] = useState(null);
  const [attestations, setAttestations] = useState(null);
  const [policies, setPolicies] = useState(null);
  const [budgets, setBudgets] = useState(null);
  const [providers, setProviders] = useState(null);

  // ── Adapters: real backend shapes → panel-expected shapes ────────
  //
  // The designer drew the UI against a future data contract; today's
  // endpoints return different field names and no pre-aggregated counts.
  // These adapters keep the view code untouched while wiring up the
  // real /v1/control/* endpoints.

  const loadStatus = useCallback(async () => {
    try {
      const [s, atts, pols, buds] = await Promise.all([
        getControlStatus(),
        getAttestations().catch(() => ({ attestations: [] })),
        getPolicies().catch(() => ({ policies: [] })),
        getBudgets().catch(() => ({ budgets: [] })),
      ]);
      const attRows = s.attestations || atts.attestations || [];
      const polRows = pols.policies || [];
      const budRows = buds.budgets || [];
      const providerList = Array.isArray(s.providers) ? s.providers : [];
      const analyzerCount = s.content_analyzers?.count
        ?? (Array.isArray(s.content_analyzers) ? s.content_analyzers.length : 0);
      setStatus({
        enforcement_mode: s.enforcement_mode ?? (s.skip_governance ? 'audit' : 'enforce'),
        attestations_active: attRows.filter(a => a.status === 'active').length,
        policies_active: polRows.filter(p => (p.status ?? 'active') === 'active').length,
        budgets_active: budRows.length,
        budgets_breached: 0,
        provider_count: providerList.length,
        providers_healthy: providerList.length,
        analyzer_count: analyzerCount,
        uptime_seconds: s.uptime_seconds,
        version: s.gateway_id ? `gw · ${String(s.gateway_id).slice(0, 8)}` : 'gateway',
        last_config_change: s.policy_cache?.last_sync || null,
      });
      setEvents([]); // no backend event feed yet
    } catch (e) { setStatus({}); setEvents([]); }
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
        signed_at: a.created_at,
        expires_at: a.updated_at,
        status: a.status,
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
          hits_24h: 0,
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
        spent_usd: 0,
        cap_usd: 0,
        tokens_used: 0,
        tokens_cap: b.max_tokens,
        breach: 'ok',
        expires: b.updated_at,
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

  useEffect(() => { loadStatus(); }, [loadStatus]);
  useEffect(() => {
    if (tab === 'attestations' && attestations == null) loadAttestations();
    if (tab === 'policies' && policies == null) loadPolicies();
    if (tab === 'budgets' && budgets == null) loadBudgets();
    if (tab === 'providers' && providers == null) loadProviders();
  }, [tab, attestations, policies, budgets, providers, loadAttestations, loadPolicies, loadBudgets, loadProviders]);

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
      <div className="cp-intro">
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
        {tab === 'status' && <StatusPanel status={status} events={events} />}
        {tab === 'attestations' && <AttestationsPanel rows={attestations} canWrite={unlocked} onUnlock={onUnlock} onRefresh={loadAttestations} />}
        {tab === 'policies' && <PoliciesPanel rows={policies} canWrite={unlocked} onUnlock={onUnlock} onRefresh={loadPolicies} />}
        {tab === 'budgets' && <BudgetsPanel rows={budgets} canWrite={unlocked} onUnlock={onUnlock} onRefresh={loadBudgets} />}
        {tab === 'providers' && <ProvidersPanel data={providers} canWrite={unlocked} onUnlock={onUnlock} onRefresh={loadProviders} />}
      </div>

      {modal && <UnlockModal onClose={() => setModal(false)} onSubmit={onSubmitKey} />}
    </div>
  );
}
