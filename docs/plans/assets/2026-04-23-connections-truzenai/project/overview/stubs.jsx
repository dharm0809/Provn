/* Walacor Gateway — Stub views for nav completeness.
   Each view has distinct structure so the nav feels real, but they
   explicitly call out "coming next" so the user knows where to focus. */

function StubScaffold({ icon, title, subtitle, children }) {
  return (
    <div className="stub-view">
      <div className="stub-hero card card-accent">
        <div className="stub-hero-inner">
          <div className="stub-icon-wrap">
            <div className="stub-icon">{icon}</div>
          </div>
          <div className="stub-hero-text">
            <div className="stub-eyebrow">◆ WALACOR GATEWAY · COMING NEXT</div>
            <h1 className="stub-title">{title}</h1>
            <p className="stub-subtitle">{subtitle}</p>
          </div>
        </div>
      </div>
      {children}
    </div>
  );
}

// ── Sessions ──────────────────────────────────────────────
function SessionsStub() {
  const rows = MockData.genSessions().concat(MockData.genSessions()).slice(0, 12);
  return (
    <StubScaffold
      icon={<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M3 6h18M3 12h18M3 18h12"/></svg>}
      title="Sessions"
      subtitle="Browse and inspect every session threaded through the gateway. Drill into individual request timelines, replay governance decisions, and verify chain integrity per session.">
      <div className="card">
        <div className="intel-card-head">
          <div>
            <div className="intel-card-title">All Sessions (preview)</div>
            <div className="intel-card-sub">12 of 1,842 sessions · showing most recent</div>
          </div>
          <div className="intel-card-actions">
            <input className="form-input" placeholder="filter by session id, user, model…" style={{ width: 260 }} readOnly />
            <button className="btn-wal btn-ghost btn-sm">Export</button>
          </div>
        </div>
        <table className="verdict-table">
          <thead>
            <tr>
              <th>Session ID</th>
              <th>User</th>
              <th>Model</th>
              <th>Records</th>
              <th>Chain</th>
              <th>Duration</th>
              <th>Last Activity</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s, i) => (
              <tr key={s.session_id + i} className="verdict-row">
                <td className="mono" style={{ color: 'var(--gold)' }}>{formatSessionId(s.session_id)}</td>
                <td>user_{Math.floor(Math.random()*999)}</td>
                <td className="mono small">{s.model}</td>
                <td className="mono">{s.record_count}</td>
                <td><span className="chain-chip chain-ok">◆ verified</span></td>
                <td className="mono small">{Math.round(Math.random()*20+1)}m {Math.round(Math.random()*59)}s</td>
                <td className="small txt-muted">{timeAgo(s.last_activity)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </StubScaffold>
  );
}

// ── Attempts ──────────────────────────────────────────────
function AttemptsStub() {
  const rows = MockData.genActivity().concat(MockData.genActivity()).slice(0, 15);
  return (
    <StubScaffold
      icon={<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M2 12h3l3-7 4 14 3-7h7"/></svg>}
      title="Attempts"
      subtitle="Every request — allowed, blocked, or errored. Filter by disposition, model, path, user, or analyzer verdict. This is where auditors live.">
      <div className="card">
        <div className="intel-card-head">
          <div>
            <div className="intel-card-title">Request Stream (preview)</div>
            <div className="intel-card-sub">showing latest 15 · 340,219 in window</div>
          </div>
          <div className="intel-card-actions">
            <div className="intel-tab-group">
              {['ALL', 'ALLOW', 'BLOCK', 'ERROR'].map(f => (
                <button key={f} className={`intel-tab-sm${f === 'ALL' ? ' active' : ''}`}>{f}</button>
              ))}
            </div>
          </div>
        </div>
        <table className="verdict-table">
          <thead>
            <tr>
              <th>Disp</th>
              <th>Model</th>
              <th>Method · Path</th>
              <th>Latency</th>
              <th>Tokens</th>
              <th>Analyzers</th>
              <th>Time</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a, i) => {
              const meta = dispositionMeta(a.disposition);
              return (
                <tr key={a.execution_id + i} className="verdict-row">
                  <td><span className={`disposition-badge ${meta.cls}`}>{meta.label}</span></td>
                  <td className="mono small">{a.model_id}</td>
                  <td className="mono small"><span style={{ color: 'var(--blue)' }}>{a.method}</span> {a.path}</td>
                  <td className="mono small">{Math.round(Math.random()*800+120)}ms</td>
                  <td className="mono small">{Math.round(Math.random()*3000+400)}</td>
                  <td>
                    <span className="chain-chip chain-ok" style={{ marginRight: 4 }}>◆ 7/7</span>
                  </td>
                  <td className="small txt-muted">{timeAgo(a.timestamp)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </StubScaffold>
  );
}

// ── Control ──────────────────────────────────────────────
function ControlStub() {
  const policies = [
    { name: 'block_pii_exfiltration', status: 'active', analyzers: 3, denials_24h: 142, severity: 'critical' },
    { name: 'enforce_model_allowlist', status: 'active', analyzers: 1, denials_24h: 28, severity: 'high' },
    { name: 'rate_limit_per_user', status: 'active', analyzers: 1, denials_24h: 86, severity: 'medium' },
    { name: 'budget_enforcement', status: 'active', analyzers: 1, denials_24h: 12, severity: 'high' },
    { name: 'block_prompt_injection', status: 'active', analyzers: 2, denials_24h: 204, severity: 'critical' },
    { name: 'redact_secrets_outbound', status: 'shadow', analyzers: 1, denials_24h: 0, severity: 'medium' },
    { name: 'require_audit_tag', status: 'disabled', analyzers: 1, denials_24h: 0, severity: 'low' },
  ];
  return (
    <StubScaffold
      icon={<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><path d="M3 6h6m4 0h8M3 12h4m4 0h10M3 18h2m4 0h12"/><circle cx="11" cy="6" r="2" fill="currentColor"/><circle cx="9" cy="12" r="2" fill="currentColor"/><circle cx="7" cy="18" r="2" fill="currentColor"/></svg>}
      title="Policy Control"
      subtitle="Policies, analyzers, allowlists, budgets, and enforcement mode. Changes here write directly to the audit chain and take effect within 30 seconds.">
      <div className="card">
        <div className="intel-card-head">
          <div>
            <div className="intel-card-title">Active Policies</div>
            <div className="intel-card-sub">7 policies · 5 active · 1 shadow · 1 disabled</div>
          </div>
          <div className="intel-card-actions">
            <button className="btn-wal btn-ghost btn-sm">+ New Policy</button>
            <button className="btn-wal btn-primary btn-sm">Mode: ENFORCED</button>
          </div>
        </div>
        <div className="control-grid">
          {policies.map(p => (
            <div key={p.name} className={`control-row control-${p.status}`}>
              <div className="control-row-left">
                <span className={`control-dot control-dot-${p.status}`} />
                <div>
                  <div className="control-name mono">{p.name}</div>
                  <div className="control-meta small txt-muted">{p.analyzers} analyzer{p.analyzers > 1 ? 's' : ''} · severity <span className={`sev sev-${p.severity}`}>{p.severity}</span></div>
                </div>
              </div>
              <div className="control-row-right">
                <div className="control-metric">
                  <div className="small txt-muted">denials · 24h</div>
                  <div className="mono control-metric-val">{p.denials_24h}</div>
                </div>
                <span className={`badge-wal badge-${p.status === 'active' ? 'pass' : p.status === 'shadow' ? 'warn' : 'muted'}`}>
                  {p.status.toUpperCase()}
                </span>
                <button className="btn-wal btn-ghost btn-sm">Edit</button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </StubScaffold>
  );
}

// ── Compliance ──────────────────────────────────────────────
function ComplianceStub() {
  const frameworks = [
    { id: 'eu_ai_act', label: 'EU AI Act', score: 88, grade: 'B', gaps: 2 },
    { id: 'nist', label: 'NIST AI RMF', score: 92, grade: 'A', gaps: 1 },
    { id: 'soc2', label: 'SOC 2 Type II', score: 95, grade: 'A', gaps: 0 },
    { id: 'iso42001', label: 'ISO 42001', score: 79, grade: 'C', gaps: 4 },
  ];
  const gradeColor = (g) => ({ A: 'var(--green)', B: 'var(--gold)', C: 'var(--amber)', D: 'var(--red)', F: 'var(--red)' })[g] || 'var(--text-muted)';
  return (
    <StubScaffold
      icon={<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2L3 6v6c0 5 4 9 9 11 5-2 9-6 9-11V6l-9-4z"/><path d="M8 12l3 3 5-6"/></svg>}
      title="Compliance"
      subtitle="Audit-ready reports mapped to EU AI Act, NIST AI RMF, SOC 2, and ISO 42001. Every control maps back to chain evidence — no gap is hand-waved.">
      <div className="compliance-grid">
        {frameworks.map(f => (
          <div key={f.id} className="card compliance-card">
            <div className="compliance-head">
              <div className="compliance-label">{f.label}</div>
              <span className="badge-wal badge-muted mono">{f.id}</span>
            </div>
            <div className="compliance-score">
              <div className="compliance-score-val" style={{ color: gradeColor(f.grade) }}>{f.score}</div>
              <div className="compliance-grade" style={{ color: gradeColor(f.grade) }}>{f.grade}</div>
            </div>
            <div className="compliance-meta">
              {f.gaps === 0
                ? <span style={{ color: 'var(--green)' }}>✓ 0 gaps identified</span>
                : <span style={{ color: 'var(--amber)' }}>⚠ {f.gaps} gap{f.gaps > 1 ? 's' : ''} to close</span>}
            </div>
            <div className="compliance-actions">
              <button className="btn-wal btn-ghost btn-sm">Preview</button>
              <div className="compliance-downloads">
                <button className="dl-btn">JSON</button>
                <button className="dl-btn">CSV</button>
                <button className="dl-btn">PDF</button>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="intel-card-head">
          <div>
            <div className="intel-card-title">Chain Integrity</div>
            <div className="intel-card-sub">continuous verification across all sessions</div>
          </div>
          <span className="chain-chip chain-ok">◆ 1,842 / 1,842 VERIFIED</span>
        </div>
        <div style={{ padding: '8px 2px 2px', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)', letterSpacing: '0.06em' }}>
          Last verification: 2 minutes ago · depth 247,019 records · merkle root{' '}
          <span style={{ color: 'var(--gold)' }}>sha256:4a7b…e01c</span>
        </div>
      </div>
    </StubScaffold>
  );
}

// ── Playground ──────────────────────────────────────────────
function PlaygroundStub() {
  return (
    <StubScaffold
      icon={<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6l8 6-8 6M13 18h8"/></svg>}
      title="Playground"
      subtitle="Test prompts against any provisioned model. Every request here generates a real audit record, so you can inspect governance decisions in isolation.">
      <div className="pg-grid">
        <div className="card pg-left">
          <div className="pg-form-head">◆ Prompt</div>
          <div className="pg-form-row">
            <label>Model</label>
            <select className="form-input"><option>claude-sonnet-4.5</option><option>gpt-4.1</option><option>gemini-2.5-pro</option></select>
          </div>
          <div className="pg-form-row">
            <label>System</label>
            <textarea className="form-input pg-textarea" rows={2} placeholder="You are a helpful assistant…" />
          </div>
          <div className="pg-form-row">
            <label>User prompt</label>
            <textarea className="form-input pg-textarea" rows={5} placeholder="Type your prompt here…" defaultValue="Summarize the governance posture for our AI gateway in 3 bullets." />
          </div>
          <div className="pg-form-row-inline">
            <div>
              <label className="small">Creativity</label>
              <input type="range" min="0" max="2" step="0.1" defaultValue="0.7" />
            </div>
            <div>
              <label className="small">Max tokens</label>
              <input className="form-input mono" style={{ width: 100 }} defaultValue="1024" />
            </div>
            <button className="btn-wal btn-primary">▶ Send <span className="small" style={{ opacity: 0.6, marginLeft: 4 }}>⌘↵</span></button>
          </div>
        </div>

        <div className="card pg-right">
          <div className="pg-form-head">◇ Response</div>
          <div className="pg-response-preview">
            <p>The AI gateway enforces policy-level access controls across all inbound requests, with redaction and blocking paths wired to three active analyzers.</p>
            <p>Chain-of-custody is preserved through Walacor; 1,842 sessions verified in the current window with zero integrity failures.</p>
            <p>Two candidate models are currently in shadow validation; one has passed its automated gate and is ready for review.</p>
          </div>
          <div className="pg-governance">
            <div className="pg-gov-title">◆ GOVERNANCE READOUT</div>
            <div className="pg-gov-grid">
              <span className="pg-gov-k">EXEC</span><span className="pg-gov-v mono">exec_8a91b4c2e5f0</span>
              <span className="pg-gov-k">ATTEST</span><span className="pg-gov-v mono">att_3f7de1a8</span>
              <span className="pg-gov-k">POLICY</span><span className="pg-gov-v"><span className="badge-wal badge-pass">allow</span></span>
              <span className="pg-gov-k">CHAIN</span><span className="pg-gov-v mono">seq #247,019</span>
              <span className="pg-gov-k">LATENCY</span><span className="pg-gov-v mono">342ms</span>
              <span className="pg-gov-k">TOKENS</span><span className="pg-gov-v mono">82 in / 147 out</span>
              <span className="pg-gov-k">ANALYSIS</span><span className="pg-gov-v">
                <span className="badge-wal badge-pass">safe</span>{' '}
                <span className="badge-wal badge-pass">no_pii</span>{' '}
                <span className="badge-wal badge-pass">within_budget</span>
              </span>
            </div>
          </div>
        </div>
      </div>
    </StubScaffold>
  );
}

window.SessionsStub = SessionsStub;
window.AttemptsStub = AttemptsStub;
window.ControlStub = ControlStub;
window.ComplianceStub = ComplianceStub;
window.PlaygroundStub = PlaygroundStub;
