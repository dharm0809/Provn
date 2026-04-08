import { useState, useEffect, useCallback, useMemo } from 'react';
import * as api from '../api';
import { formatNumber, formatTime, formatUptime } from '../utils';

function SortArrow({ col, sortCol, sortDir }) {
  return (
    <span className={`sort-arrow${sortCol === col ? ' active' : ''}`}>
      {sortCol === col ? (sortDir === 'asc' ? '▲' : '▼') : '▼'}
    </span>
  );
}

// ─── Auth Gate ──────────────────────────────────────────────────

function AuthGate({ onAuth }) {
  const [key, setKey] = useState('');
  const [error, setError] = useState('');

  const tryAuth = async () => {
    if (!key.trim()) { setError('Please enter an API key'); return; }
    api.setControlKey(key.trim());
    try {
      await api.getControlStatus();
      onAuth();
    } catch {
      api.clearControlKey();
      setError('Invalid API key or gateway unreachable');
    }
  };

  return (
    <div className="auth-card">
      <div style={{ fontSize: 28, color: 'var(--gold)', marginBottom: 16 }}>◆</div>
      <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>Control Plane Access</div>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 28, lineHeight: 1.5 }}>
        Enter your gateway API key to manage models, policies, and budgets.
      </div>
      <div className="form-group">
        <input type="password" className="form-input" placeholder="API key" value={key}
          onChange={e => setKey(e.target.value)} onKeyDown={e => e.key === 'Enter' && tryAuth()} autoFocus />
      </div>
      <div className="form-actions" style={{ justifyContent: 'center' }}>
        <button className="btn-primary" onClick={tryAuth}>Authenticate</button>
      </div>
      {error && <div style={{ fontSize: 12, color: 'var(--red)', textAlign: 'center', marginTop: 12 }}>{error}</div>}
    </div>
  );
}

function ConfirmDialog({ title, message, item, onConfirm, onCancel }) {
  return (
    <div className="confirm-overlay" onClick={onCancel}>
      <div className="confirm-dialog" onClick={e => e.stopPropagation()}>
        <h3>{title}</h3>
        {item && <div className="confirm-item">{item}</div>}
        <p>{message}</p>
        <div className="confirm-actions">
          <button className="btn" onClick={onCancel}>Cancel</button>
          <button className="btn-danger" onClick={onConfirm}>Confirm</button>
        </div>
      </div>
    </div>
  );
}

// ─── Models Sub-View ────────────────────────────────────────────

function ModelsView({ refresh }) {
  const [attestations, setAttestations] = useState([]);
  const [modelCaps, setModelCaps] = useState({});
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ model_id: '', provider: 'ollama', endpoint: '', notes: '' });
  const [loading, setLoading] = useState(true);
  const [discovered, setDiscovered] = useState(null);
  const [discovering, setDiscovering] = useState(false);
  const [registeringAll, setRegisteringAll] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(null);
  const [sortCol, setSortCol] = useState('model_id');
  const [sortDir, setSortDir] = useState('asc');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [attData, health] = await Promise.all([api.getAttestations(), api.getHealth()]);
      setAttestations(attData.attestations || []);
      setModelCaps(health.model_capabilities || {});
    } catch (e) { if (e.message === 'AUTH') refresh(); }
    finally { setLoading(false); }
  }, [refresh]);

  useEffect(() => { load(); }, [load]);

  const toggleSort = (col) => {
    if (sortCol === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortCol(col); setSortDir('asc'); }
  };

  const sortedAttestations = useMemo(() => {
    return [...attestations].sort((a, b) => {
      let av, bv;
      if (sortCol === 'model_id') { av = (a.model_id || '').toLowerCase(); bv = (b.model_id || '').toLowerCase(); }
      else if (sortCol === 'provider') { av = (a.provider || '').toLowerCase(); bv = (b.provider || '').toLowerCase(); }
      else if (sortCol === 'status') { av = (a.status || '').toLowerCase(); bv = (b.status || '').toLowerCase(); }
      else if (sortCol === 'verification_level') { av = (a.verification_level || '').toLowerCase(); bv = (b.verification_level || '').toLowerCase(); }
      else { av = (a.model_id || '').toLowerCase(); bv = (b.model_id || '').toLowerCase(); }
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
  }, [attestations, sortCol, sortDir]);

  const endpointFromNotes = (notes) => {
    if (!notes) return '';
    const m = String(notes).match(/(?:^|\n)\s*endpoint:\s*(.+)\s*$/i);
    return m ? m[1].trim() : '';
  };
  const notesWithoutEndpoint = (notes) => {
    if (!notes) return '';
    return String(notes).replace(/(?:^|\n)\s*endpoint:\s*.+\s*$/i, '').trim();
  };

  const submit = async () => {
    if (!form.model_id.trim() || !form.endpoint.trim()) return;
    try {
      const mergedNotes = form.notes?.trim()
        ? `endpoint: ${form.endpoint.trim()}\n${form.notes.trim()}`
        : `endpoint: ${form.endpoint.trim()}`;
      await api.createAttestation({ model_id: form.model_id, provider: form.provider, status: 'active', notes: mergedNotes });
      setShowForm(false); setForm({ model_id: '', provider: 'ollama', endpoint: '', notes: '' }); load();
    } catch (e) { if (e.message === 'AUTH') refresh(); }
  };

  const runDiscovery = async () => {
    setDiscovering(true);
    try {
      const data = await api.discoverModels();
      setDiscovered(data.models || []);
    } catch (e) { if (e.message === 'AUTH') refresh(); }
    finally { setDiscovering(false); }
  };

  const registerModel = async (m) => {
    try {
      await api.createAttestation({ model_id: m.model_id, provider: m.provider, status: 'active', notes: `Discovered from ${m.source}` });
      // Update discovered list to reflect registration
      setDiscovered(prev => prev ? prev.map(d => d.model_id === m.model_id ? { ...d, registered: true } : d) : prev);
      load();
    } catch (e) { if (e.message === 'AUTH') refresh(); }
  };

  const registerAllUnregistered = async () => {
    if (!discovered) return;
    const unregistered = discovered.filter(m => !m.registered);
    if (unregistered.length === 0) return;
    setRegisteringAll(true);
    try {
      for (const m of unregistered) {
        await api.createAttestation({ model_id: m.model_id, provider: m.provider, status: 'active', notes: `Discovered from ${m.source}` });
      }
      setDiscovered(prev => prev ? prev.map(d => ({ ...d, registered: true })) : prev);
      load();
    } catch (e) { if (e.message === 'AUTH') refresh(); }
    finally { setRegisteringAll(false); }
  };

  if (loading) return <div className="skeleton-block" style={{ height: 200 }} />;

  const attModels = new Set(attestations.map(a => a.model_id));
  const unregisteredCount = discovered ? discovered.filter(m => !m.registered).length : 0;

  return (
    <>
      <div className="card">
        <div className="card-head">
          <span className="card-title">Model Attestations ({attestations.length})</span>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn btn-sm" onClick={runDiscovery} disabled={discovering}>
              {discovering ? 'Scanning...' : 'Discover Models'}
            </button>
            <button className="btn-primary btn-sm" onClick={() => setShowForm(!showForm)}>+ Add Model</button>
          </div>
        </div>

        <div style={{ padding: '0 16px 8px', fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.5 }}>
          Only models registered here can be accessed through the gateway. Unregistered models are blocked.
        </div>

        {showForm && (
          <div className="inline-form">
            <div className="inline-form-title">◆ Register Model</div>
            <div className="form-row">
              <div className="form-group">
                <label className="form-label">Model ID</label>
                <input className="form-input" placeholder="e.g. qwen3:1.7b" value={form.model_id} onChange={e => setForm({ ...form, model_id: e.target.value })} />
              </div>
              <div className="form-group">
                <label className="form-label">Provider</label>
                <select className="form-select" value={form.provider} onChange={e => setForm({ ...form, provider: e.target.value })}>
                  <option value="ollama">ollama</option>
                  <option value="openai">openai</option>
                  <option value="anthropic">anthropic</option>
                  <option value="huggingface">huggingface</option>
                </select>
              </div>
            </div>
            <div className="form-group">
              <label className="form-label">Model Endpoint (required)</label>
              <input
                className="form-input"
                placeholder="e.g. http://localhost:11434 or 10.0.0.5:8080"
                value={form.endpoint}
                onChange={e => setForm({ ...form, endpoint: e.target.value })}
              />
            </div>
            <div className="form-group">
              <label className="form-label">Notes</label>
              <input className="form-input" placeholder="Optional notes" value={form.notes} onChange={e => setForm({ ...form, notes: e.target.value })} />
            </div>
            <div className="form-actions">
              <button className="btn-primary" onClick={submit} disabled={!form.model_id.trim() || !form.endpoint.trim()}>Register</button>
              <button className="btn-ghost" onClick={() => setShowForm(false)}>Cancel</button>
            </div>
          </div>
        )}

        {attestations.length === 0 ? (
          <div className="empty-state"><p>No models registered. Use "Discover Models" to scan providers or add manually.</p></div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th className="sortable" onClick={() => toggleSort('model_id')}>Model ID <SortArrow col="model_id" sortCol={sortCol} sortDir={sortDir} /></th>
                <th className="sortable" onClick={() => toggleSort('provider')}>Provider <SortArrow col="provider" sortCol={sortCol} sortDir={sortDir} /></th>
                <th className="sortable" onClick={() => toggleSort('status')}>Status <SortArrow col="status" sortCol={sortCol} sortDir={sortDir} /></th>
                <th className="sortable" onClick={() => toggleSort('verification_level')}>Verification <SortArrow col="verification_level" sortCol={sortCol} sortDir={sortDir} /></th>
                <th>Endpoint</th><th>Notes</th><th style={{ textAlign: 'right' }}>Actions</th>
              </tr></thead>
              <tbody>
                {sortedAttestations.map(a => (
                  <tr key={a.attestation_id}>
                    <td className="id">{a.model_id}</td>
                    <td className="mono">{a.provider}</td>
                    <td><span className={`badge ${a.status === 'active' ? 'badge-pass' : a.status === 'revoked' ? 'badge-fail' : 'badge-warn'}`}>{a.status}</span></td>
                    <td><span className="badge badge-muted">{a.verification_level}</span></td>
                    <td className="mono" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{endpointFromNotes(a.notes) || '-'}</td>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{notesWithoutEndpoint(a.notes) || '-'}</td>
                    <td>
                      <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                        {a.status === 'active'
                          ? <button className="btn-ghost btn-sm" onClick={async () => { try { await api.revokeAttestation(a.attestation_id); load(); } catch (e) { if (e.message === 'AUTH') refresh(); } }}>Revoke</button>
                          : <button className="btn-ghost btn-sm" onClick={async () => { try { await api.approveAttestation({ model_id: a.model_id, provider: a.provider, status: 'active' }); load(); } catch (e) { if (e.message === 'AUTH') refresh(); } }}>Approve</button>
                        }
                        <button className="btn-danger btn-sm" onClick={() => setConfirmRemove(a)}>Remove</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {discovered !== null && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-head">
            <span className="card-title">Discovered Models ({discovered.length})</span>
            <div style={{ display: 'flex', gap: 8 }}>
              {unregisteredCount > 0 && (
                <button className="btn-primary btn-sm" onClick={registerAllUnregistered} disabled={registeringAll}>
                  {registeringAll ? 'Registering...' : `Register All (${unregisteredCount})`}
                </button>
              )}
              <button className="btn-ghost btn-sm" onClick={() => setDiscovered(null)}>Dismiss</button>
            </div>
          </div>
          {discovered.length === 0 ? (
            <div className="empty-state"><p>No models found from configured providers.</p></div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Model ID</th><th>Provider</th><th>Source</th><th>Status</th><th style={{ textAlign: 'right' }}>Actions</th></tr></thead>
                <tbody>
                  {discovered.map(m => (
                    <tr key={m.model_id}>
                      <td className="id">{m.model_id}</td>
                      <td className="mono">{m.provider}</td>
                      <td><span className="badge badge-muted">{m.source}</span></td>
                      <td>
                        {m.registered
                          ? <span className="badge badge-pass">Registered</span>
                          : <span className="badge badge-warn">Unregistered</span>
                        }
                      </td>
                      <td>
                        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                          {!m.registered && (
                            <button className="btn-primary btn-sm" onClick={() => registerModel(m)}>Register</button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
      {confirmRemove && (
        <ConfirmDialog
          title="Remove Model"
          item={confirmRemove.model_id}
          message="This will permanently remove this model attestation. The model will be blocked from serving requests."
          onCancel={() => setConfirmRemove(null)}
          onConfirm={async () => {
            try { await api.removeAttestation(confirmRemove.attestation_id); setConfirmRemove(null); load(); }
            catch (e) { if (e.message === 'AUTH') refresh(); }
          }}
        />
      )}
    </>
  );
}

// ─── Policies Sub-View ──────────────────────────────────────────

function PoliciesView({ refresh }) {
  const [policies, setPolicies] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ policy_name: '', enforcement_level: 'blocking', description: '' });
  const [rules, setRules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [sortCol, setSortCol] = useState('policy_name');
  const [sortDir, setSortDir] = useState('asc');

  const load = useCallback(async () => {
    setLoading(true);
    try { const data = await api.getPolicies(); setPolicies(data.policies || []); }
    catch (e) { if (e.message === 'AUTH') refresh(); }
    finally { setLoading(false); }
  }, [refresh]);

  useEffect(() => { load(); }, [load]);

  const toggleSort = (col) => {
    if (sortCol === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortCol(col); setSortDir('asc'); }
  };

  const sortedPolicies = useMemo(() => {
    return [...policies].sort((a, b) => {
      let av, bv;
      if (sortCol === 'policy_name') { av = (a.policy_name || '').toLowerCase(); bv = (b.policy_name || '').toLowerCase(); }
      else if (sortCol === 'enforcement_level') { av = (a.enforcement_level || '').toLowerCase(); bv = (b.enforcement_level || '').toLowerCase(); }
      else if (sortCol === 'rules') {
        av = (a.rules || []).length + (a.prompt_rules || []).length + (a.rag_rules || []).length;
        bv = (b.rules || []).length + (b.prompt_rules || []).length + (b.rag_rules || []).length;
      }
      else if (sortCol === 'status') { av = (a.status || '').toLowerCase(); bv = (b.status || '').toLowerCase(); }
      else { av = (a.policy_name || '').toLowerCase(); bv = (b.policy_name || '').toLowerCase(); }
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
  }, [policies, sortCol, sortDir]);

  const submit = async () => {
    if (!form.policy_name.trim()) return;
    try {
      await api.createPolicy({ ...form, rules: rules.filter(r => r.field && r.value) });
      setShowForm(false); setForm({ policy_name: '', enforcement_level: 'blocking', description: '' }); setRules([]); load();
    } catch (e) { if (e.message === 'AUTH') refresh(); }
  };

  if (loading) return <div className="skeleton-block" style={{ height: 200 }} />;

  return (
    <div className="card">
      <div className="card-head">
        <span className="card-title">Policies ({policies.length})</span>
        <button className="btn-primary btn-sm" onClick={() => setShowForm(!showForm)}>+ Add Policy</button>
      </div>

      {showForm && (
        <div className="inline-form">
          <div className="inline-form-title">◆ Create Policy</div>
          <div className="form-row">
            <div className="form-group">
              <label className="form-label">Policy Name</label>
              <input className="form-input" placeholder="e.g. content-safety" value={form.policy_name} onChange={e => setForm({ ...form, policy_name: e.target.value })} />
            </div>
            <div className="form-group">
              <label className="form-label">Enforcement Level</label>
              <select className="form-select" value={form.enforcement_level} onChange={e => setForm({ ...form, enforcement_level: e.target.value })}>
                <option value="blocking">blocking</option>
                <option value="audit_only">audit_only</option>
              </select>
            </div>
          </div>
          <div className="form-group">
            <label className="form-label">Description</label>
            <textarea className="form-textarea" placeholder="What does this policy enforce?" value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} />
          </div>
          <div className="form-group">
            <label className="form-label">Rules</label>
            {rules.map((r, i) => (
              <div key={i} className="rule-row">
                <input className="form-input" placeholder="field" value={r.field} onChange={e => { const n = [...rules]; n[i].field = e.target.value; setRules(n); }} />
                <select className="form-select" value={r.operator} onChange={e => { const n = [...rules]; n[i].operator = e.target.value; setRules(n); }}>
                  {['equals', 'contains', 'not_equals', 'regex'].map(v => <option key={v} value={v}>{v}</option>)}
                </select>
                <input className="form-input" placeholder="value" value={r.value} onChange={e => { const n = [...rules]; n[i].value = e.target.value; setRules(n); }} />
                <button className="rule-remove" onClick={() => setRules(rules.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
            <button className="btn-ghost" onClick={() => setRules([...rules, { field: '', operator: 'equals', value: '' }])}>+ Add Rule</button>
          </div>
          <div className="form-actions">
            <button className="btn-primary" onClick={submit}>Create Policy</button>
            <button className="btn-ghost" onClick={() => setShowForm(false)}>Cancel</button>
          </div>
        </div>
      )}

      {policies.length === 0 ? (
        <div className="empty-state"><p>No policies defined. Create a policy to enforce governance rules.</p></div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr>
              <th className="sortable" onClick={() => toggleSort('policy_name')}>Name <SortArrow col="policy_name" sortCol={sortCol} sortDir={sortDir} /></th>
              <th className="sortable" onClick={() => toggleSort('enforcement_level')}>Enforcement <SortArrow col="enforcement_level" sortCol={sortCol} sortDir={sortDir} /></th>
              <th className="sortable" onClick={() => toggleSort('rules')}>Rules <SortArrow col="rules" sortCol={sortCol} sortDir={sortDir} /></th>
              <th className="sortable" onClick={() => toggleSort('status')}>Status <SortArrow col="status" sortCol={sortCol} sortDir={sortDir} /></th>
              <th style={{ textAlign: 'right' }}>Actions</th>
            </tr></thead>
            <tbody>
              {sortedPolicies.map(p => {
                const ruleCount = (p.rules || []).length + (p.prompt_rules || []).length + (p.rag_rules || []).length;
                return (
                  <tr key={p.policy_id}>
                    <td style={{ fontWeight: 500 }}>{p.policy_name}</td>
                    <td><span className={`badge ${p.enforcement_level === 'blocking' ? 'badge-enforced' : 'badge-muted'}`}>{p.enforcement_level}</span></td>
                    <td className="mono">{ruleCount}</td>
                    <td><span className={`badge ${p.status === 'active' ? 'badge-pass' : 'badge-muted'}`}>{p.status}</span></td>
                    <td>
                      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                        <button className="btn-danger btn-sm" onClick={() => setConfirmDelete(p)}>Delete</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {confirmDelete && (
        <ConfirmDialog
          title="Delete Policy"
          item={confirmDelete.policy_name}
          message="This will permanently delete this policy. Governance rules in this policy will no longer be enforced."
          onCancel={() => setConfirmDelete(null)}
          onConfirm={async () => {
            try { await api.deletePolicy(confirmDelete.policy_id); setConfirmDelete(null); load(); }
            catch (e) { if (e.message === 'AUTH') refresh(); }
          }}
        />
      )}
    </div>
  );
}

// ─── Budgets Sub-View ───────────────────────────────────────────

function BudgetsView({ refresh, health }) {
  const [budgets, setBudgets] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ tenant_id: '', user: '', period: 'monthly', max_tokens: '' });
  const [loading, setLoading] = useState(true);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [sortCol, setSortCol] = useState('tenant_id');
  const [sortDir, setSortDir] = useState('asc');

  const load = useCallback(async () => {
    setLoading(true);
    try { const data = await api.getBudgets(); setBudgets(data.budgets || []); }
    catch (e) { if (e.message === 'AUTH') refresh(); }
    finally { setLoading(false); }
  }, [refresh]);

  useEffect(() => { load(); }, [load]);

  const toggleSort = (col) => {
    if (sortCol === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortCol(col); setSortDir('asc'); }
  };

  const sortedBudgets = useMemo(() => {
    return [...budgets].sort((a, b) => {
      let av, bv;
      if (sortCol === 'tenant_id') { av = (a.tenant_id || '').toLowerCase(); bv = (b.tenant_id || '').toLowerCase(); }
      else if (sortCol === 'user') { av = (a.user || '').toLowerCase(); bv = (b.user || '').toLowerCase(); }
      else if (sortCol === 'period') { av = (a.period || '').toLowerCase(); bv = (b.period || '').toLowerCase(); }
      else if (sortCol === 'max_tokens') { av = a.max_tokens || 0; bv = b.max_tokens || 0; }
      else { av = (a.tenant_id || '').toLowerCase(); bv = (b.tenant_id || '').toLowerCase(); }
      if (av < bv) return sortDir === 'asc' ? -1 : 1;
      if (av > bv) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
  }, [budgets, sortCol, sortDir]);

  const submit = async () => {
    const max = parseInt(form.max_tokens);
    if (!max || max <= 0) return;
    try {
      await api.createBudget({ tenant_id: form.tenant_id, user: form.user, period: form.period, max_tokens: max });
      setShowForm(false); setForm({ tenant_id: '', user: '', period: 'monthly', max_tokens: '' }); load();
    } catch (e) { if (e.message === 'AUTH') refresh(); }
  };

  if (loading) return <div className="skeleton-block" style={{ height: 200 }} />;

  const tokenBudget = health?.token_budget;
  const pct = tokenBudget?.percent_used || 0;
  const barCls = pct < 60 ? 'green' : pct < 85 ? 'amber' : 'red';

  return (
    <>
      <div className="card">
        <div className="card-head">
          <span className="card-title">Token Budgets ({budgets.length})</span>
          <button className="btn-primary btn-sm" onClick={() => setShowForm(!showForm)}>+ Add Budget</button>
        </div>

        {showForm && (
          <div className="inline-form">
            <div className="inline-form-title">◆ Create Budget</div>
            <div className="form-row">
              <div className="form-group">
                <label className="form-label">Tenant ID</label>
                <input className="form-input" placeholder="e.g. dev-tenant" value={form.tenant_id} onChange={e => setForm({ ...form, tenant_id: e.target.value })} />
              </div>
              <div className="form-group">
                <label className="form-label">User (optional)</label>
                <input className="form-input" placeholder="Leave empty for tenant-wide" value={form.user} onChange={e => setForm({ ...form, user: e.target.value })} />
              </div>
            </div>
            <div className="form-row">
              <div className="form-group">
                <label className="form-label">Period</label>
                <select className="form-select" value={form.period} onChange={e => setForm({ ...form, period: e.target.value })}>
                  <option value="monthly">monthly</option>
                  <option value="daily">daily</option>
                </select>
              </div>
              <div className="form-group">
                <label className="form-label">Max Tokens</label>
                <input type="number" className="form-input" placeholder="e.g. 1000000" value={form.max_tokens} onChange={e => setForm({ ...form, max_tokens: e.target.value })} />
              </div>
            </div>
            <div className="form-actions">
              <button className="btn-primary" onClick={submit}>Create Budget</button>
              <button className="btn-ghost" onClick={() => setShowForm(false)}>Cancel</button>
            </div>
          </div>
        )}

        {tokenBudget && (
          <div style={{ marginBottom: 16 }}>
            <div className="form-label" style={{ marginBottom: 8 }}>Current Usage</div>
            <div className="progress-bar"><div className={`progress-fill ${barCls}`} style={{ width: `${Math.min(pct, 100)}%` }} /></div>
            <div className="progress-label">
              <span>{formatNumber(tokenBudget.tokens_used)} used</span>
              <span>{formatNumber(tokenBudget.max_tokens)} limit ({pct.toFixed(1)}%)</span>
            </div>
          </div>
        )}

        {budgets.length === 0 ? (
          <div className="empty-state"><p>No budgets configured.</p></div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th className="sortable" onClick={() => toggleSort('tenant_id')}>Tenant <SortArrow col="tenant_id" sortCol={sortCol} sortDir={sortDir} /></th>
                <th className="sortable" onClick={() => toggleSort('user')}>User <SortArrow col="user" sortCol={sortCol} sortDir={sortDir} /></th>
                <th className="sortable" onClick={() => toggleSort('period')}>Period <SortArrow col="period" sortCol={sortCol} sortDir={sortDir} /></th>
                <th className="sortable" onClick={() => toggleSort('max_tokens')}>Max Tokens <SortArrow col="max_tokens" sortCol={sortCol} sortDir={sortDir} /></th>
                <th style={{ textAlign: 'right' }}>Actions</th>
              </tr></thead>
              <tbody>
                {sortedBudgets.map(b => (
                  <tr key={b.budget_id}>
                    <td className="mono">{b.tenant_id}</td>
                    <td className="mono">{b.user || '(all)'}</td>
                    <td><span className="badge badge-muted">{b.period}</span></td>
                    <td className="mono">{formatNumber(b.max_tokens)}</td>
                    <td>
                      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                        <button className="btn-danger btn-sm" onClick={() => setConfirmDelete(b)}>Delete</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {confirmDelete && (
        <ConfirmDialog
          title="Delete Budget"
          item={`${confirmDelete.tenant_id}${confirmDelete.user ? ' / ' + confirmDelete.user : ''} — ${confirmDelete.period}`}
          message="This will permanently remove this token budget. Usage limits will no longer be enforced for this scope."
          onCancel={() => setConfirmDelete(null)}
          onConfirm={async () => {
            try { await api.deleteBudget(confirmDelete.budget_id); setConfirmDelete(null); load(); }
            catch (e) { if (e.message === 'AUTH') refresh(); }
          }}
        />
      )}
    </>
  );
}

// ─── Status Sub-View ────────────────────────────────────────────

function StatusView({ refresh, health }) {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try { setStatus(await api.getControlStatus()); }
      catch (e) { if (e.message === 'AUTH') refresh(); }
      finally { setLoading(false); }
    })();
  }, [refresh]);

  if (loading) return <div className="skeleton-block" style={{ height: 300 }} />;
  if (!status) return null;

  const s = status;
  const caps = s.model_capabilities || health?.model_capabilities || {};
  const capKeys = Object.keys(caps);
  const provs = s.providers || [];
  const StatusRow = ({ label, children }) => (
    <div className="status-row">
      <div className="status-row-label">{label}</div>
      <div className="status-row-value">{children}</div>
    </div>
  );

  return (
    <div className="status-plain">
      <section className="status-section">
        <h3 className="status-section-title">Gateway</h3>
        <StatusRow label="Gateway ID"><span className="mono">{s.gateway_id || '-'}</span></StatusRow>
        <StatusRow label="Tenant">{s.tenant_id || '-'}</StatusRow>
        <StatusRow label="Enforcement">{s.enforcement_mode || '-'}</StatusRow>
        <StatusRow label="Sync Mode">{s.sync_mode || '-'}</StatusRow>
        <StatusRow label="Uptime">{formatUptime(s.uptime_seconds || 0)}</StatusRow>
      </section>

      <section className="status-section">
        <h3 className="status-section-title">Caches</h3>
        {s.attestation_cache && <StatusRow label="Attestation Entries">{s.attestation_cache.entries}</StatusRow>}
        {s.policy_cache && (
          <>
            <StatusRow label="Policy Version">{s.policy_cache.version}</StatusRow>
            <StatusRow label="Policy Stale">{s.policy_cache.stale ? 'YES' : 'no'}</StatusRow>
            {s.policy_cache.last_sync && <StatusRow label="Last Sync">{formatTime(s.policy_cache.last_sync)}</StatusRow>}
          </>
        )}
      </section>

      {s.wal && (
        <section className="status-section">
          <h3 className="status-section-title">WAL Storage</h3>
          <StatusRow label="Pending Records">{s.wal.pending_records}</StatusRow>
          <StatusRow label="Disk Usage">{formatNumber(s.wal.disk_usage_bytes)} bytes</StatusRow>
        </section>
      )}

      <section className="status-section">
        <h3 className="status-section-title">Auth & Security</h3>
        <StatusRow label="Auth Mode">{s.auth_mode || 'api_key'}</StatusRow>
        <StatusRow label="JWT Configured">{s.jwt_configured ? 'yes' : 'no'}</StatusRow>
        {s.content_analyzers ? (
          <>
            <StatusRow label="Content Analyzers">{s.content_analyzers.count}</StatusRow>
            <StatusRow label="Analyzer Types">{s.content_analyzers.types?.join(', ') || '-'}</StatusRow>
          </>
        ) : (
          <StatusRow label="Content Analyzers">0</StatusRow>
        )}
        <StatusRow label="Lineage">{s.lineage_enabled ? 'enabled' : 'disabled'}</StatusRow>
      </section>

      {(s.session_chain || s.token_budget || s.model_routes_count != null) && (
        <section className="status-section">
          <h3 className="status-section-title">Runtime State</h3>
          {s.session_chain && <StatusRow label="Active Sessions">{s.session_chain.active_sessions}</StatusRow>}
          {s.token_budget && (
            <>
              <StatusRow label="Tokens Used">{formatNumber(s.token_budget.tokens_used || 0)}</StatusRow>
              <StatusRow label="Max Tokens">{formatNumber(s.token_budget.max_tokens || 0)}</StatusRow>
            </>
          )}
          {s.model_routes_count != null && <StatusRow label="Model Routes">{s.model_routes_count}</StatusRow>}
        </section>
      )}

      {capKeys.length > 0 && (
        <section className="status-section">
          <h3 className="status-section-title">Model Capabilities</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Model</th><th>Supports Tools</th></tr></thead>
              <tbody>
                {capKeys.map(mid => (
                  <tr key={mid}>
                    <td className="id">{mid}</td>
                    <td><span className={`badge ${caps[mid].supports_tools ? 'badge-pass' : 'badge-muted'}`}>{caps[mid].supports_tools ? 'yes' : 'no'}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {provs.length > 0 && (
        <section className="status-section">
          <h3 className="status-section-title">Configured Providers</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Provider</th><th>URL</th></tr></thead>
              <tbody>
                {provs.map((p, i) => (
                  <tr key={i}><td className="mono">{p.name}</td><td className="mono" style={{ fontSize: 12 }}>{p.url}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {s.onnx_models?.length > 0 && (
        <section className="status-section">
          <h3 className="status-section-title">ONNX Intelligence Models</h3>
          {s.onnx_models.map((m, i) => (
            <div key={i} className="status-inline-item" style={{ borderBottom: i < s.onnx_models.length - 1 ? '1px solid var(--border)' : 'none' }}>
              <span className={`badge ${m.loaded ? 'badge-pass' : 'badge-fail'}`} style={{ fontSize: 11, flexShrink: 0 }}>
                {m.loaded ? 'LOADED' : 'OFFLINE'}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{m.name}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>{m.purpose}</div>
                <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>
                  <span className="mono">{m.type}</span>
                  {m.labels > 0 && <span style={{ marginLeft: 8 }}>{m.labels} classes</span>}
                </div>
              </div>
            </div>
          ))}
        </section>
      )}

      {s.intelligence && (
        <section className="status-section">
          <h3 className="status-section-title">Intelligence Engine</h3>
          {s.intelligence.anomaly_detector && (
            <StatusRow label="Anomaly Detector">
              {s.intelligence.anomaly_detector.models_tracked} models tracked · {s.intelligence.anomaly_detector.total_records_analyzed} records analyzed
            </StatusRow>
          )}
          {s.intelligence.consistency_tracker && (
            <StatusRow label="Consistency Tracker">
              {s.intelligence.consistency_tracker.total_pairs_stored} pairs · {s.intelligence.consistency_tracker.total_comparisons} comparisons · {s.intelligence.consistency_tracker.recent_inconsistencies} inconsistencies
            </StatusRow>
          )}
          {s.intelligence.consistency_tracker?.model_reliability && Object.keys(s.intelligence.consistency_tracker.model_reliability).length > 0 && (
            <StatusRow label="Model Reliability">
              {Object.entries(s.intelligence.consistency_tracker.model_reliability).map(([model, rel]) => (
                <div key={model} style={{ marginBottom: 4 }}>
                  <span className="mono" style={{ fontSize: 12 }}>{model}</span>
                  <span className={`badge ${rel.score >= 0.9 ? 'badge-pass' : rel.score >= 0.7 ? 'badge-warn' : 'badge-fail'}`} style={{ fontSize: 10, marginLeft: 6 }}>
                    {(rel.score * 100).toFixed(0)}%
                  </span>
                  <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 6 }}>({rel.comparisons} comparisons)</span>
                </div>
              ))}
            </StatusRow>
          )}
          {s.intelligence.field_registry && (
            <StatusRow label="Schema Overflow">
              {s.intelligence.field_registry.tracked_fields} fields tracked · {s.intelligence.field_registry.promotion_candidates} promotion candidates
            </StatusRow>
          )}
          {s.intelligence.background_worker && (
            <StatusRow label="LLM Worker">
              <span className={`badge ${s.intelligence.background_worker.running ? 'badge-pass' : 'badge-muted'}`} style={{ fontSize: 10 }}>
                {s.intelligence.background_worker.running ? 'RUNNING' : 'STOPPED'}
              </span>
              {' '}{s.intelligence.background_worker.processed} processed · {s.intelligence.background_worker.distillation_samples} distillation samples
              {s.intelligence.background_worker.queue_size > 0 && (
                <span style={{ marginLeft: 6, color: 'var(--gold)' }}>({s.intelligence.background_worker.queue_size} queued)</span>
              )}
            </StatusRow>
          )}
        </section>
      )}

      <section className="status-section">
        <h3 className="status-section-title">Health</h3>
        <StatusRow label="Status">{health?.status || '-'}</StatusRow>
        <StatusRow label="Uptime">{formatUptime(health?.uptime_seconds || 0)}</StatusRow>
        {health?.session_chain && <StatusRow label="Active Sessions">{health.session_chain.active_sessions}</StatusRow>}
        {health?.token_budget && <StatusRow label="Token Usage">{health.token_budget.tokens_used} / {health.token_budget.max_tokens}</StatusRow>}
      </section>
    </div>
  );
}

// ─── Main Control View ──────────────────────────────────────────

export default function Control({ navigate, params = {}, health }) {
  const [authed, setAuthed] = useState(api.hasControlKey());
  const [sub, setSub] = useState(params.sub || 'models');

  const refresh = useCallback(() => { setAuthed(false); }, []);

  if (!authed) return <AuthGate onAuth={() => setAuthed(true)} />;

  const subTabs = ['models', 'governance', 'status'];

  return (
    <div className="fade-child">
      <div className="control-subnav">
        {subTabs.map(t => (
          <button key={t} className={`control-subtab${sub === t ? ' active' : ''}`} onClick={() => setSub(t)}>
            {t === 'governance' ? 'Policies & Budgets' : t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>
      {sub === 'models' && <ModelsView refresh={refresh} />}
      {sub === 'governance' && (
        <div style={{ display: 'grid', gap: 16 }}>
          <PoliciesView refresh={refresh} />
          <BudgetsView refresh={refresh} health={health} />
        </div>
      )}
      {sub === 'status' && <StatusView refresh={refresh} health={health} />}
    </div>
  );
}
